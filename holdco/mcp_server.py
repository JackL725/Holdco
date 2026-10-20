"""Holdco as an MCP server (Phase 4, docs/phases/phase-4-scale.md section 1).

One handler, handle(message, client), serves both transports and both protocol generations the Phase 1 client
(holdco/mcp.py) speaks: the stateless 2026-07-28 generation (server/discover, the namespaced _meta keys in every
request, resultType in every result) and the older one (initialize, notifications/initialized, ping).

    POST /mcp on the dashboard port       server.Handler checks the password and the Origin, then http_reply()
                                          answers: one message or a batch in, JSON out (never a stream)
    python -m holdco.mcp_server           stdio: server/discover, initialize and ping are answered here; every
                                          other request goes to http://127.0.0.1:<HOLDCO_PORT>/mcp, so one
                                          process writes the database and the audit log

Eight tools, every call as the owner: list_companies, get_company_brain, list_inbox, decide, create_task,
get_metrics, latest_brief and doctor. The writes call the dashboard's own functions (actions.decide, the task
route's contract check), so the kill switch, taint, trust and every gate rule apply; decide's audit row names
"mcp:<clientInfo.name>". Every result passes vault.redact, so no secret leaves through a tool.

At load time this module imports only the standard library and holdco.mcp, so the stdio proxy never opens the
database; the tools import what they need when they run, inside the dashboard.
"""
from __future__ import annotations

import base64
import copy
import datetime as _dt
import http.client
import json
import os
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.request

from . import __version__
from .mcp import (CURRENT, FALLBACK, META_CAPABILITIES, META_CLIENT, META_SERVER, META_VERSION, OLDER_ACCEPTED,
                  UNSUPPORTED_VERSION, _header_value, _target_name)

SERVER_INFO = {"name": "holdco", "version": __version__}
CAPABILITIES = {"tools": {}}
TOOLS_TTL_MS = 60_000          # tools/list may be cached this long ...
CACHE_SCOPE = "client"         # ... by each client for itself
PATH = "/mcp"
MAX_BODY = 2_000_000           # bytes of one POST /mcp body
FORWARD_TIMEOUT = 300.0        # seconds the stdio proxy waits for the dashboard (approving a deploy can take a while)
INBOX_LIMIT = 100              # pending rows list_inbox returns, like the dashboard's Inbox
METRIC_ROWS = 500
DEFAULT_DAYS = 30
CREATED_BY = "owner (mcp)"     # tasks.created_by of a task an MCP client created

NOT_RUNNING = "Holdco is not running. Start it (start_windows.bat or ./start_linux.sh) and try again."
WRONG_PASSWORD = ("Holdco refused the password. Set HOLDCO_PASSWORD in this MCP client's configuration to the "
                  "dashboard password.")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
LOCAL_METHODS = ("server/discover", "initialize", "ping")   # what the stdio proxy answers without the dashboard


class ToolFailure(Exception):
    """A refusal or error inside a tool: the client gets it as an isError result, never a JSON-RPC error."""


class _RPCError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# ---------------------------------------------------------------- the tools' definitions

def _int_prop(description: str, minimum: int | None = None, maximum: int | None = None) -> dict:
    out: dict = {"type": "integer", "description": description}
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


def _str_prop(description: str, enum: tuple | None = None) -> dict:
    out: dict = {"type": "string", "description": description}
    if enum:
        out["enum"] = list(enum)
    return out


def _tool(name: str, title: str, description: str, properties: dict, required: tuple = (), writes: bool = False,
          destructive: bool = False, outside: bool = False) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "annotations": {"title": title, "readOnlyHint": not writes, "destructiveHint": destructive,
                            "idempotentHint": not writes, "openWorldHint": outside}}


COMPANY_ID = _int_prop("The company's id (list_companies shows them)", 1)
TOOLS = [
    _tool("list_companies", "List companies",
          "Every company in the Holdco portfolio with its status, stage and mission, its task and agent counts, "
          "its active goals (current of target) and how many requests wait for the owner. Start here to find "
          "company ids.", {}),
    _tool("get_company_brain", "Company brain",
          "A company's brain: the digest every agent reads first (strategy, goals, latest metrics with their "
          "source, decisions, experiments, lessons, contacts, proposals) as text, and every section in full as "
          "structured content.", {"company_id": COMPANY_ID}, ("company_id",)),
    _tool("list_inbox", "List the inbox",
          "What waits for the owner: approval requests (with the risk tag and what would be sent), questions and "
          "decisions (with the options, the recommended option and the deadline), and why Holdco asked, for "
          "example because the session read outside content. Omit company_id for every company.",
          {"company_id": _int_prop("Only this company's items (optional)", 1)}),
    _tool("decide", "Decide an inbox item",
          "Decide one inbox item as the owner, exactly as the dashboard's Inbox does: approve or reject a request "
          "(approving runs it, so it may send an email, publish or spend), or answer a question or a decision "
          "with decision 'answer' and the answer (one of its options). The kill switch and every gate rule "
          "apply. Call list_inbox first.",
          {"approval_id": _int_prop("The inbox item's id (list_inbox)", 1),
           "decision": _str_prop("approve, reject or answer", ("approve", "reject", "answer")),
           "feedback": _str_prop("A note for the team (optional; a rejection with feedback becomes a revision "
                                 "task)"),
           "answer": _str_prop("The answer to a question or a decision, usually one of its options")},
          ("approval_id", "decision"), writes=True, destructive=True, outside=True),
    _tool("create_task", "Create a task",
          "Create a task for a company's team as the owner, with the same checks as the dashboard's task form. "
          "The scheduler hands it to the agent at the next tick.",
          {"company_id": COMPANY_ID,
           "title": _str_prop("Short imperative title"),
           "description": _str_prop("Objective, inputs and what is out of scope"),
           "assign_to": _str_prop("Role (ceo, engineering, marketing, ...) or the agent's name; the CEO when "
                                  "omitted"),
           "deliverable": _str_prop("Where the result will be: a file path like site/index.html, or an action"),
           "acceptance": _str_prop("Checkable acceptance criteria, one per line"),
           "goal_id": _int_prop("The active goal this task serves (get_company_brain lists them)"),
           "priority": _int_prop("1 = urgent (the default), 2 = normal, 3 = later", 1, 3),
           "due_days": _int_prop("Due in this many days (1 to 90)", 1, 90),
           "checks": _str_prop("Tool checks to run when it is done, one per line, e.g. browser_check "
                               "site/index.html")},
          ("company_id", "title"), writes=True),
    _tool("get_metrics", "Get metrics",
          "A company's metrics, newest first (up to 500), each with its source: a connector key such as stripe "
          "when a connected service reported it, or manual when an agent typed it.",
          {"company_id": COMPANY_ID, "name": _str_prop("Only this metric, e.g. stripe.revenue (optional)"),
           "days": _int_prop("How many days back (default 30)", 1)},
          ("company_id",)),
    _tool("latest_brief", "Latest brief",
          "The newest brief: a company's when company_id is given, else the portfolio brief. kind is daily, "
          "weekly or monthly (any kind when omitted).",
          {"company_id": _int_prop("The company (optional; omitted means the portfolio brief)"),
           "kind": _str_prop("daily, weekly or monthly (optional)", ("daily", "weekly", "monthly"))}),
    _tool("doctor", "Health checks",
          "Holdco's health checks as on the Doctor page (model server, search, connectors, sandbox, disk, "
          "backups, errors) with the summary of the latest run.", {}),
]


def tool_list() -> list[dict]:
    return copy.deepcopy(TOOLS)


# ---------------------------------------------------------------- one message

def _error(rid, code: int, message: str, data=None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _client_name(client: dict | None) -> str:
    name = "".join(ch for ch in str((client or {}).get("name") or "") if ch.isprintable()).strip()
    return name[:60] or "unknown"


def _note_client(client: dict, info) -> None:
    if isinstance(info, dict) and str(info.get("name") or "").strip():
        client["name"] = str(info["name"])
        client["info"] = {k: info[k] for k in ("name", "version", "title") if isinstance(info.get(k), str)}


def handle(message: dict, client: dict | None = None) -> dict | None:
    """One JSON-RPC message in, its reply out (None for a notification). client is the connection's state, which
    handle() keeps: the client's name (for the audit log), whether it sent initialize and which older version it
    agreed. Never raises."""
    client = client if isinstance(client, dict) else {}
    try:
        return _handle(message, client)
    except Exception as e:   # a bug, not a refusal: the traceback goes to the console, the client gets -32603
        traceback.print_exc()
        if isinstance(message, dict) and "id" in message:
            return _error(message.get("id"), INTERNAL_ERROR, f"Internal error ({type(e).__name__})")
        return None


def _handle(message, client: dict) -> dict | None:
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid request: a JSON-RPC message is an object")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        if "result" in message or "error" in message:
            return None   # a client's reply to a request of ours (Holdco never sends one)
        return _error(message.get("id"), INVALID_REQUEST, "Invalid request: the message has no method")
    if "id" not in message:
        return None   # notifications (notifications/initialized among them) need no reply
    rid = message["id"]
    params = message.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return _error(rid, INVALID_PARAMS, "Invalid params: params must be an object")
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    new = META_VERSION in meta   # the current generation names its version in every request
    if new:
        _note_client(client, meta.get(META_CLIENT))
        version = meta.get(META_VERSION)
        if version != CURRENT:
            return _error(rid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                          {"supported": [CURRENT], "requested": version})
    try:
        if method == "server/discover":
            result = {"protocolVersions": [CURRENT], "capabilities": copy.deepcopy(CAPABILITIES),
                      "serverInfo": dict(SERVER_INFO)}
        elif method == "initialize":
            if new:
                raise _RPCError(METHOD_NOT_FOUND, "Method not found: initialize (2026-07-28 has no handshake; "
                                                  "use server/discover)")
            result = _initialize(params, client)
        elif method == "ping":
            if new:
                raise _RPCError(METHOD_NOT_FOUND, "Method not found: ping (removed in 2026-07-28)")
            if not client.get("initialized"):
                raise _RPCError(INVALID_REQUEST, "Invalid request: send initialize before ping")
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list(), "ttlMs": TOOLS_TTL_MS, "cacheScope": CACHE_SCOPE}
        elif method == "tools/call":
            result = _call(params, client)
        else:
            raise _RPCError(METHOD_NOT_FOUND, f"Method not found: {method}")
    except _RPCError as e:
        return _error(rid, e.code, e.message, e.data)
    if new or method == "server/discover":
        # Holdco answers every request at once, so a result is always complete (never input_required).
        result = {**result, "resultType": "complete", "_meta": {META_SERVER: dict(SERVER_INFO)}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _initialize(params: dict, client: dict) -> dict:
    asked = params.get("protocolVersion")
    version = asked if asked in OLDER_ACCEPTED else FALLBACK
    _note_client(client, params.get("clientInfo"))
    if isinstance(params.get("capabilities"), dict):
        client["capabilities"] = params["capabilities"]
    client.update(protocol=version, initialized=True)
    return {"protocolVersion": version, "capabilities": copy.deepcopy(CAPABILITIES), "serverInfo": dict(SERVER_INFO)}


def _call(params: dict, client: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments")
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise _RPCError(INVALID_PARAMS, "Invalid params: arguments must be an object")
    fn = _TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if fn is None:
        raise _RPCError(INVALID_PARAMS, f"Unknown tool: {name}. tools/list names Holdco's eight tools.")
    try:
        text, data = fn(args, client)
        return _tool_result(text, data, False)
    except ToolFailure as e:
        return _tool_result(str(e), None, True)
    except Exception as e:   # a bug: the traceback goes to the console, the client reads the error
        traceback.print_exc()
        return _tool_result(f"Holdco could not run {name}: {type(e).__name__}: {e}", None, True)


def _redact(text: str) -> str:
    try:
        from . import vault
        return vault.redact(text)
    except Exception:
        return text


def _scrubbed(value):
    """A result with every known secret value replaced by [redacted] (vault.redact on every string)."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {_redact(str(k)): _scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(str(value))


def _tool_result(text: str | None, data: dict | None, is_error: bool) -> dict:
    data = _scrubbed(data) if data is not None else None
    text = _redact(text) if text is not None else json.dumps(data, ensure_ascii=False, indent=1, default=str)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": data if data is not None else {"error": text}, "isError": is_error}


# ---------------------------------------------------------------- the tools

def _int_arg(args: dict, key: str, default=None, required: bool = False, low: int | None = None,
             high: int | None = None):
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ToolFailure(f"{key} is required (a whole number).")
        return default
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
        v = int(v.strip())
    if not isinstance(v, int) or isinstance(v, bool):
        raise ToolFailure(f"{key} must be a whole number, not {v!r}.")
    if (low is not None and v < low) or (high is not None and v > high):
        raise ToolFailure(f"{key} must be from {low} to {high}." if high is not None
                          else f"{key} must be {low} or more.")
    return v


def _str_arg(args: dict, key: str) -> str:
    v = args.get(key)
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x) for x in v)
    return str(v)


def _company(cid: int) -> dict:
    """A company the owner sees (a Holdco Bench company is temporary and never the owner's)."""
    from . import db
    c = db.get_company(cid)
    if not c or c.get("status") == "bench":
        raise ToolFailure(f"Company #{cid} not found. list_companies shows the companies and their ids.")
    return c


class _Route:
    """Stands in for a dashboard route's handler and URL match, so a tool returns what the route returns."""

    def __init__(self, *groups: str):
        self._groups = groups
        self.query: dict = {}

    def group(self, i: int) -> str:
        return self._groups[i - 1]


def _list_companies(args: dict, client: dict):
    from . import brain, db, server
    companies = []
    for c in db.q("SELECT * FROM companies WHERE status!='bench' ORDER BY status='archived', id"):
        stats = server._stats(c["id"])   # the numbers /api/state shows
        goals = [{k: g.get(k) for k in ("id", "title", "metric", "current", "target", "deadline", "days_left",
                                        "owner_role")} for g in brain.active_goals(c["id"])]
        companies.append({"id": c["id"], "name": c["name"], "status": c["status"], "stage": c["stage"],
                          "mission": c.get("mission") or "", "idea": c.get("idea") or "",
                          "created_at": c.get("created_at"), "stats": stats, "goals": goals,
                          "pending_approvals": stats["approvals"]})
    s = db.get_settings()
    return None, {"companies": companies, "pending_approvals": sum(c["pending_approvals"] for c in companies),
                  "kill_switch": bool(s.get("kill_switch")), "scheduler_enabled": bool(s.get("scheduler_enabled"))}


def _get_company_brain(args: dict, client: dict):
    from . import brain
    c = _company(_int_arg(args, "company_id", required=True))
    data = brain.api_brain(_Route(), _Route(str(c["id"])), None)   # the Brain tab's data, digest included
    return data["digest"], {"company_id": c["id"], "company": c["name"], **data}


def _inbox_item(r: dict) -> dict:
    from . import actions, db
    p = db.loads(r.get("payload"), {})
    p = p if isinstance(p, dict) else {"value": p}
    action = r["action"]
    item = {"id": r["id"], "company_id": r["company_id"], "company": r.get("company_name") or "",
            "kind": action if action in ("question", "decision") else "request", "action": action,
            "label": actions.ACTION_LABELS.get(action, action),
            "tag": p.get("tag") if action == "connector_call" else actions.TAG_OF_ACTION.get(action, ""),
            "summary": r.get("summary") or "", "requested_by": r.get("agent_name") or "",
            "role": r.get("agent_role") or "", "task_id": r.get("task_id"), "created_at": r.get("created_at"),
            "question": p.get("question") or "", "options": p.get("options") or [],
            "recommended": p.get("recommended") or "", "deadline": p.get("deadline_at") or "",
            # Phase 3: why the owner is asked at all (a tainted session, the trust ladder) and the outside sources
            "why_asked": p.get("_why_asked") or "", "taint": p.get("_taint") or [], "payload": p}
    if action == "decision":
        item["reversible"] = bool(p.get("reversible", True))
    return item


def _list_inbox(args: dict, client: dict):
    from . import db
    cid = _int_arg(args, "company_id", default=0)
    if cid:
        _company(cid)
    sql = ("SELECT p.*, a.name AS agent_name, a.role AS agent_role, c.name AS company_name FROM approvals p "
           "LEFT JOIN agents a ON a.id=p.agent_id LEFT JOIN companies c ON c.id=p.company_id "
           "WHERE p.status='pending' AND COALESCE(c.status, '')!='bench'")
    params: list = []
    if cid:
        sql += " AND p.company_id=?"
        params.append(cid)
    items = [_inbox_item(r) for r in db.q(sql + f" ORDER BY p.id DESC LIMIT {INBOX_LIMIT}", params)]
    return None, {"inbox": items, "count": len(items), "kill_switch": bool(db.get_settings().get("kill_switch"))}


def _decide(args: dict, client: dict):
    from . import actions, db
    aid = _int_arg(args, "approval_id", required=True)
    decision = str(args.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject", "answer"):
        raise ToolFailure("decision must be approve, reject or answer.")
    feedback, answer = _str_arg(args, "feedback").strip(), _str_arg(args, "answer").strip()
    row = db.one("SELECT p.*, c.status AS company_status FROM approvals p LEFT JOIN companies c "
                 "ON c.id=p.company_id WHERE p.id=?", (aid,))
    if not row or row.get("company_status") in (None, "bench"):
        raise ToolFailure(f"Request #{aid} not found. list_inbox shows what is waiting.")
    label = actions.ACTION_LABELS.get(row["action"], row["action"])
    asks = row["action"] in ("question", "decision")
    if decision == "answer" and not asks:
        raise ToolFailure(f"Request #{aid} is a request ({label}), not a question: approve or reject it.")
    if asks and decision != "reject" and not answer:
        p = db.loads(row["payload"], {}) or {}
        options = [str(o) for o in (p.get("options") or [])] if isinstance(p, dict) else []
        hint = f" Options: {'; '.join(options)}." if options else ""
        if options and p.get("recommended"):
            hint += f" Recommended: {p['recommended']}."
        raise ToolFailure(f"#{aid} is a {row['action']}: answer it with decision 'answer' and the answer.{hint}")
    who = "mcp:" + _client_name(client)
    try:
        out = actions.decide(aid, "reject" if decision == "reject" else "approve", None, feedback, answer,
                             decided_by="mcp", approved_by=who)
    except ValueError as e:   # already decided, the kill switch, a request that no longer exists
        raise ToolFailure(str(e)) from None
    result = out.get("result") or ""
    data = {"approval": {k: out.get(k) for k in ("id", "company_id", "action", "summary", "status", "result",
                                                  "feedback", "decided_by", "decided_at")},
            "label": label, "approved_by": who if out.get("status") in ("done", "failed") else None}
    if out.get("status") == "failed":
        raise ToolFailure(f"Request #{aid} ({label}) was approved but failed: {result}. The dashboard's Inbox can "
                          f"retry it.")
    if asks:
        text = (f"{row['action'].title()} #{aid} dismissed." if out.get("status") == "rejected"
                else f"{row['action'].title()} #{aid} answered: {result}")
    else:
        text = f"Request #{aid} ({label}) {out.get('status')}." + (f" Outcome: {result}" if result else "")
    return text, data


def _assignee(company: dict, wanted: str) -> dict:
    from . import db
    team = db.q("SELECT * FROM agents WHERE company_id=? ORDER BY id", (company["id"],))
    key = (wanted or "").strip().lower()
    if not key:
        ceo = next((a for a in team if a["role"] == "ceo"), None)
        if not ceo:
            raise ToolFailure(f"{company['name']} has no CEO agent; name a role with assign_to.")
        return ceo
    found = next((a for a in team if a["enabled"] and (a["role"] == key or (a["name"] or "").strip().lower() == key)),
                 None)
    if not found:
        roles = sorted({a["role"] for a in team if a["enabled"]})
        raise ToolFailure(f"No teammate with the role or name '{wanted}' in {company['name']}. Team roles: "
                          f"{', '.join(roles) or 'none'}.")
    return found


def _create_task(args: dict, client: dict):
    from . import bus, db, server, truth
    from .routing import HTTPError
    c = _company(_int_arg(args, "company_id", required=True))
    title = _str_arg(args, "title").strip()
    if not title:
        raise ToolFailure("Give the task a title.")
    agent = _assignee(c, _str_arg(args, "assign_to"))
    priority = max(1, min(_int_arg(args, "priority", default=1), 3))
    contract_in = {k: (_str_arg(args, k) if k == "checks" else args[k]) for k in ("goal_id", "due_days", "checks")
                   if args.get(k) not in (None, "")}
    try:
        contract = server._contract(c["id"], contract_in)   # the task form's own check (400 there, isError here)
    except HTTPError as e:
        raise ToolFailure(e.message) from None
    description, deliverable = _str_arg(args, "description"), _str_arg(args, "deliverable")
    ts = db.now()
    cols = {"company_id": c["id"], "title": title[:200], "description": description, "role": agent["role"],
            "agent_id": agent["id"], "status": "todo", "priority": priority, "created_by": CREATED_BY,
            "deliverable": deliverable.strip()[:200], "acceptance": _str_arg(args, "acceptance").strip()[:2000],
            "kind": truth.classify_task(title, description, deliverable, ""), "created_at": ts, "updated_at": ts,
            **contract}
    tid = db.run(f"INSERT INTO tasks({', '.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
    bus.emit(c["id"], "task", f"created task #{tid} for {agent['name']}: {title}", None,
             {"task_id": tid, "by": CREATED_BY})
    return (f"Created task #{tid} in {c['name']} for {agent['name']} ({agent['role']}): {title}",
            {"task": server._task_out(db.get_task(tid))})


def _get_metrics(args: dict, client: dict):
    from . import db
    c = _company(_int_arg(args, "company_id", required=True))
    name = _str_arg(args, "name").strip()
    days = _int_arg(args, "days", default=DEFAULT_DAYS, low=1)
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=min(days, 36500))).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = ("SELECT id, name, value, note, created_at, COALESCE(NULLIF(source, ''), 'manual') AS source FROM metrics "
           "WHERE company_id=? AND created_at>=?")
    params: list = [c["id"], since]
    if name:
        sql += " AND name=?"
        params.append(name)
    rows = db.q(sql + f" ORDER BY created_at DESC, id DESC LIMIT {METRIC_ROWS}", params)
    return None, {"company_id": c["id"], "company": c["name"], "name": name or None, "days": days, "metrics": rows}


def _latest_brief(args: dict, client: dict):
    from . import db, rhythms
    cid = _int_arg(args, "company_id", default=0)
    kind = _str_arg(args, "kind").strip().lower()
    if kind and kind not in rhythms.BRIEF_KINDS:
        raise ToolFailure(f"kind must be {', '.join(rhythms.BRIEF_KINDS[:-1])} or {rhythms.BRIEF_KINDS[-1]}.")
    whose = _company(cid)["name"] if cid else "The portfolio"
    sql, params = ("SELECT * FROM briefs WHERE company_id=?", [cid]) if cid else \
        ("SELECT * FROM briefs WHERE company_id IS NULL", [])
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    row = db.one(sql + " ORDER BY id DESC LIMIT 1", params)
    if not row:
        return f"{whose} has no {kind + ' ' if kind else ''}brief yet.", {"brief": None}
    row["data"] = db.loads(row.get("data"), {})
    return f"{whose}: {row['kind']} brief #{row['id']}, {row['created_at']}\n\n{row['body'] or ''}", {"brief": row}


def _doctor(args: dict, client: dict):
    from . import doctor
    rows = [dict(r) for r in list(doctor.LAST)]
    summary = doctor.summary(rows)
    if not rows:
        return ("The Doctor has not run yet (it runs within a minute of starting Holdco, then every hour).",
                {"checks": [], "summary": summary})
    lines = [f"Doctor: {summary['ok']} ok, {summary['warn']} warn, {summary['bad']} bad "
             f"(checked {summary['checked_at']})"]
    lines += [f"- [{r.get('status')}] {r.get('label')}: {r.get('detail') or ''}".rstrip(": ") for r in rows]
    return "\n".join(lines), {"checks": rows, "summary": summary}


_TOOLS_BY_NAME = {"list_companies": _list_companies, "get_company_brain": _get_company_brain,
                  "list_inbox": _list_inbox, "decide": _decide, "create_task": _create_task,
                  "get_metrics": _get_metrics, "latest_brief": _latest_brief, "doctor": _doctor}


# ---------------------------------------------------------------- Streamable HTTP (POST /mcp)

def _header_matches(header: str, value) -> bool:
    if value is None:
        return False
    header, value = header.strip(), str(value)
    return header in (value, _header_value(value))


def _header_problem(message, headers) -> str | None:
    """The optional Mcp-Method and Mcp-Name headers must describe the message in the body."""
    if not isinstance(message, dict) or "id" not in message:
        return None
    method, name = headers.get("Mcp-Method"), headers.get("Mcp-Name")
    if method is not None and not _header_matches(method, message.get("method")):
        return "Invalid request: the Mcp-Method header does not match the body's method"
    if name is not None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if not _header_matches(name, _target_name(str(message.get("method") or ""), params)):
            return "Invalid request: the Mcp-Name header does not match the body"
    return None


def _ua_name(user_agent: str) -> str:
    """'claude-code/2.1 (cli)' -> 'claude-code': who an older-generation request comes from, since it carries no
    clientInfo after initialize and Holdco keeps no session."""
    return (user_agent or "").strip().split("/", 1)[0].split(" ", 1)[0][:60]


def http_reply(method: str, headers, body: bytes) -> tuple[int, object, dict]:
    """POST, GET or DELETE /mcp after server.py checked the password and the Origin: (HTTP status, the JSON reply
    or b"" for none, extra headers). No session is issued or needed in either generation."""
    if method == "DELETE":
        return 200, {}, {}   # there is no session to end
    if method != "POST":
        return 405, {"error": "Holdco's MCP endpoint answers POST and never opens a stream"}, {"Allow": "POST, DELETE"}
    if "application/json" not in (headers.get("Content-Type") or "").lower():
        return 415, _error(None, INVALID_REQUEST, "Invalid request: send the JSON-RPC message as application/json"), {}
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, _error(None, PARSE_ERROR, "Parse error: the body is not JSON"), {}
    version = (headers.get("MCP-Protocol-Version") or "").strip()
    # An older-generation client names its agreed version in this header on every request after initialize.
    client = {"transport": "http", "name": _ua_name(headers.get("User-Agent") or ""),
              "protocol": version if version in OLDER_ACCEPTED else None, "initialized": version in OLDER_ACCEPTED}
    if isinstance(message, list):
        if not message:
            return 400, _error(None, INVALID_REQUEST, "Invalid request: the batch is empty"), {}
        replies = []
        for m in message:
            problem = _header_problem(m, headers)
            reply = _error(m.get("id"), INVALID_REQUEST, problem) if problem else handle(m, client)
            if reply is not None:
                replies.append(reply)
        return (200, replies, {}) if replies else (202, b"", {})
    problem = _header_problem(message, headers)
    if problem:
        return 400, _error(message.get("id"), INVALID_REQUEST, problem), {}
    reply = handle(message, client)
    return (202, b"", {}) if reply is None else (200, reply, {})


# ---------------------------------------------------------------- stdio (python -m holdco.mcp_server)

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 never goes through a proxy


class _Proxy:
    """Newline-delimited JSON-RPC on stdin and stdout. server/discover, initialize and ping are answered here;
    every other request is forwarded to the dashboard's /mcp, each in its own thread so a slow approval does not
    hold up the next call. An older-generation request goes out in the current generation, with the clientInfo
    from initialize, so the audit log names the client; its reply comes back without the new fields."""

    def __init__(self, port: int, password: str, stdin, stdout):
        self.url = f"http://127.0.0.1:{port}{PATH}"
        self.password = password
        self.stdin, self.stdout = stdin, stdout
        self.client: dict = {"transport": "stdio", "initialized": False}
        self._write_lock = threading.Lock()
        self._workers: list[threading.Thread] = []

    def run(self) -> int:
        for raw in iter(self.stdin.readline, b""):
            line = raw.decode("utf-8", "replace").strip()   # "\r\n" from a Windows client too
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(_error(None, PARSE_ERROR, "Parse error: the line is not JSON"))
                continue
            if isinstance(message, list):
                self._spawn(lambda m=message: self._batch(m))
            elif self._local(message):
                self._write(handle(message, self.client))
            else:
                self._spawn(lambda m=message: self._write(self._forward_safely(m)))
        for t in self._workers:   # stdin closed: let the calls in flight answer before exiting
            t.join(FORWARD_TIMEOUT + 10)
        return 0

    @staticmethod
    def _local(message) -> bool:
        return (not isinstance(message, dict) or "id" not in message or message.get("method") in LOCAL_METHODS
                or not isinstance(message.get("method"), str))

    def _spawn(self, target) -> None:
        self._workers = [t for t in self._workers if t.is_alive()]
        t = threading.Thread(target=target, name="holdco-mcp-forward", daemon=True)
        self._workers.append(t)
        t.start()

    def _batch(self, messages: list) -> None:
        if not messages:
            return self._write(_error(None, INVALID_REQUEST, "Invalid request: the batch is empty"))
        replies = [handle(m, self.client) if self._local(m) else self._forward_safely(m) for m in messages]
        replies = [r for r in replies if r is not None]
        if replies:
            self._write(replies)

    def _write(self, reply) -> None:
        if reply is None:
            return
        data = json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (OSError, ValueError):
                pass   # the client went away

    def _forward_safely(self, message: dict) -> dict:
        """_forward, with a JSON-RPC error instead of silence should it fail, so the client is never left waiting."""
        try:
            return self._forward(message)
        except Exception as e:
            traceback.print_exc()
            return _error(message.get("id"), INTERNAL_ERROR, f"Internal error in the stdio proxy ({type(e).__name__})")

    def _forward(self, message: dict) -> dict:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        old = META_VERSION not in meta
        if old:
            meta = {**meta, META_VERSION: CURRENT, META_CAPABILITIES: self.client.get("capabilities") or {},
                    META_CLIENT: self.client.get("info") or {"name": _client_name(self.client)}}
            message = {**message, "params": {**params, "_meta": meta}}
        method = str(message.get("method"))
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": f"holdco-mcp-stdio/{__version__}", "Mcp-Method": _header_value(method)}
        target = _target_name(method, message.get("params"))
        if target is not None:
            headers["Mcp-Name"] = _header_value(target)
        if self.password:
            headers["Authorization"] = "Basic " + base64.b64encode(f"holdco:{self.password}".encode()).decode()
        req = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"), method="POST",
                                     headers=headers)
        try:
            with _direct.open(req, timeout=FORWARD_TIMEOUT) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reply = json.loads(e.read().decode("utf-8"))   # a JSON-RPC error inside an HTTP error status
            except (ValueError, UnicodeDecodeError, OSError):
                reply = None
            finally:
                e.close()
            if e.code == 401 or not isinstance(reply, dict) or "error" not in reply:
                text = WRONG_PASSWORD if e.code == 401 else f"Holdco answered HTTP {e.code}."
                return self._unavailable(message, text, old)
        except (TimeoutError, socket.timeout):
            return self._unavailable(message, f"Holdco did not answer {method} within {FORWARD_TIMEOUT:g} s.", old)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                return self._unavailable(message, f"Holdco did not answer {method} within {FORWARD_TIMEOUT:g} s.",
                                         old)
            return self._unavailable(message, NOT_RUNNING, old)
        except (OSError, ValueError, http.client.HTTPException):
            return self._unavailable(message, NOT_RUNNING, old)
        if not isinstance(reply, dict):
            return self._unavailable(message, "Holdco sent a reply that is not a JSON-RPC message.", old)
        if old and isinstance(reply.get("result"), dict):
            result = {k: v for k, v in reply["result"].items() if k != "resultType"}
            rest = {k: v for k, v in (result.get("_meta") or {}).items() if k != META_SERVER} \
                if isinstance(result.get("_meta"), dict) else {}
            result.pop("_meta", None)
            if rest:
                result["_meta"] = rest
            reply = {**reply, "result": result}
        return reply

    def _unavailable(self, message: dict, text: str, old: bool) -> dict:
        """The dashboard could not answer. A tool call says why; the tool list is the same everywhere, so the
        client still sees Holdco's tools; anything else gets the answer handle() gives (method not found)."""
        if message.get("method") == "tools/call":
            result: dict = {"content": [{"type": "text", "text": text}], "structuredContent": {"error": text},
                            "isError": True}
            if not old:
                result.update(resultType="complete", _meta={META_SERVER: dict(SERVER_INFO)})
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        if not old:   # the forwarded copy carries our _meta; answer the client in the generation it spoke
            return handle(message, self.client)
        params = {k: v for k, v in (message.get("params") or {}).items() if k != "_meta"}
        return handle({**message, "params": params}, self.client)


def main() -> int:
    for stream in (sys.stderr,):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raw_port = (os.environ.get("HOLDCO_PORT") or "8420").strip()
    if not raw_port.isdigit():
        print(f"holdco MCP: HOLDCO_PORT must be a port number, not {raw_port!r}", file=sys.stderr)
        return 2
    password = os.environ.get("HOLDCO_PASSWORD", "")
    print(f"holdco MCP (stdio) {__version__}: forwarding to http://127.0.0.1:{raw_port}{PATH}", file=sys.stderr)
    try:
        return _Proxy(int(raw_port), password, sys.stdin.buffer, sys.stdout.buffer).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
