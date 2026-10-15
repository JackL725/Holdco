"""Tools the agents can call."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from . import actions, bus, checks, connectors, db, ledger, presets, roles, web, workspace

MAX_RESULT = 7000
OPEN_STATUSES = ("todo", "in_progress", "blocked", "waiting")
TASK_STATUSES = ("todo", "in_progress", "done", "blocked", "waiting", "cancelled")


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
    finish_fields: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, props: dict, required: tuple = ()):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, {"type": "object", "properties": props,
                                                  "required": list(required)}, fn)
        return fn
    return deco


def S(desc: str, **extra) -> dict:
    return {"type": "string", "description": desc, **extra}


def I(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def specs(names: list[str]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters}
            for n in names if (t := REGISTRY.get(n))]


# gpt-oss and other models sometimes call the tools they were trained with; map the obvious ones.
ALIASES = {"browser.search": "web_search", "search": "web_search", "browser.open": "fetch_url", "open": "fetch_url",
           "open_url": "fetch_url", "browse": "fetch_url", "read": "read_file", "write": "write_file",
           "python": "run_code", "container.exec": "run_code", "ls": "list_files"}
ARG_RENAMES = {"fetch_url": {"id": "url", "link": "url"}, "web_search": {"q": "query", "topn": "max_results"},
               "run_code": {"script": "code", "cmd": "code"},
               "read_file": {"file": "path", "filename": "path", "file_path": "path"},
               "write_file": {"file": "path", "filename": "path", "file_path": "path", "text": "content", "body": "content"},
               "create_task": {"role": "assign_to", "assignee": "assign_to", "details": "description"},
               "finish": {"result": "summary", "message": "summary"}}


def resolve(ctx: Ctx, name: str, args: dict) -> tuple[str, dict]:
    if name not in REGISTRY and name in ALIASES:
        name = ALIASES[name]
    renames = ARG_RENAMES.get(name, {})
    args = {renames.get(k, k): v for k, v in (args or {}).items()}
    if name == "run_code" and "code" in args and "language" not in args:
        args["language"] = "python"
    return name, args


def execute(ctx: Ctx, name: str, args: dict) -> str:
    name, args = resolve(ctx, name, args)
    t = REGISTRY.get(name)
    if not t or (ctx.allowed and name not in ctx.allowed):
        return (f"ERROR: there's no tool called '{name}' available to you. Your tools are: "
                f"{', '.join(sorted(ctx.allowed))}.")
    try:
        out = t.fn(ctx, **_clean_args(t, args))
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
    props = t.parameters["properties"]
    clean = {}
    for k, v in (args or {}).items():
        if k not in props:
            continue
        typ = props[k].get("type")
        if typ == "integer" and not isinstance(v, int):
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                continue
        elif typ == "number" and not isinstance(v, (int, float)):
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
        elif typ == "boolean" and not isinstance(v, bool):
            v = str(v).lower() in ("1", "true", "yes")
        elif typ == "string" and not isinstance(v, str):
            v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        clean[k] = v
    missing = [r for r in t.parameters.get("required", []) if r not in clean]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
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
    except Exception as e:
        return f"Search failed ({e}). Try a different query or fetch a known URL directly."
    if not results:
        return "No results. Try different words."
    return "\n\n".join(f"{i + 1}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results))


@tool("fetch_url", "Read a public web page as text.",
      {"url": S("Full http(s) URL"), "include_links": {"type": "boolean", "description": "Also list links on the page"}},
      ("url",))
def fetch_url(ctx: Ctx, url: str, include_links: bool = False) -> str:
    _log(ctx, "tool", f"read {url[:120]}")
    return web.fetch_page(url, include_links=include_links)


# ======================= files =======================

@tool("list_files", "List files in the company folder.", {"path": S("Sub-folder (default: everything)")})
def list_files(ctx: Ctx, path: str = ".") -> str:
    items = workspace.tree(ctx.company, path or ".", depth=4, limit=300)
    if not items:
        return "(empty)"
    return "\n".join(f"{i['path']}/" if i["dir"] else f"{i['path']} ({i['size']} bytes)" for i in items)


@tool("read_file", "Read a text file from the company folder.",
      {"path": S("Path relative to the company folder, e.g. research/market.md"),
       "offset": I("Character offset to start from (for long files)")}, ("path",))
def read_file(ctx: Ctx, path: str, offset: int = 0) -> str:
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
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if append and existed:
        with p.open("a", encoding="utf-8", newline="") as f:
            f.write(content)
    else:
        p.write_text(content, encoding="utf-8", newline="")
    r = workspace.rel(ctx.company, p)
    ctx.written.add(r)
    _log(ctx, "file", f"{'updated' if existed else 'created'} {r}", path=r, task_id=_task_id(ctx))
    return f"Saved {r} ({len(content)} characters)."


@tool("delete_file", "Move a file to the company's trash folder.", {"path": S("Path relative to the company folder")},
      ("path",))
def delete_file(ctx: Ctx, path: str) -> str:
    p = workspace.safe_path(ctx.company, path)
    if not p.exists() or p == workspace.root(ctx.company).resolve():
        raise ToolError(f"{path} doesn't exist.")
    dest = workspace.trash(ctx.company, p)
    _log(ctx, "file", f"moved {path} to trash")
    return f"Moved to {dest}."


# ======================= tasks =======================

def _team_roles(ctx: Ctx) -> dict[str, dict]:
    return {a["role"]: a for a in db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1", (ctx.company["id"],))}


@tool("create_task", "Create a task for a teammate (or yourself). Keep it small enough for one work session.",
      {"title": S("Short imperative title"),
       "description": S("Objective, inputs (files to read), and what's out of scope"),
       "assign_to": S("Role of the teammate (see the Team list)"),
       "deliverable": S("Where the result will be: a file path like site/index.html, or an action like 'email queued'"),
       "acceptance": S("3-5 checkable acceptance criteria, one per line"),
       "priority": I("1 = urgent, 2 = normal, 3 = later")}, ("title", "assign_to"))
def create_task(ctx: Ctx, title: str, assign_to: str, description: str = "", deliverable: str = "",
                acceptance: str = "", priority: int = 2) -> str:
    team = _team_roles(ctx)
    role = assign_to.strip().lower()
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
    ts = db.now()
    tid = db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, created_by,"
                 " deliverable, acceptance, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ctx.company["id"], title.strip()[:200], description, role, team[role]["id"], "todo",
                  max(1, min(int(priority or 2), 3)), ctx.agent["name"], deliverable.strip()[:200],
                  acceptance.strip()[:2000], ts, ts))
    _log(ctx, "task", f"created task #{tid} for {team[role]['name']}: {title}", task_id=tid)
    bus.notify("tasks", company_id=ctx.company["id"])
    tip = "" if deliverable.strip() and acceptance.strip() else \
        " Tip: tasks with a deliverable and acceptance criteria get finished far more reliably."
    return f"Created task #{tid} for {team[role]['name']} ({role}).{tip}"


@tool("update_task", "Change a task's status or add a note (e.g. cancel a stale task, unblock one, record progress).",
      {"task_id": I("Task number"), "status": S("todo, in_progress, done, blocked, waiting or cancelled"),
       "note": S("A short note to add"), "assign_to": S("Reassign to another role")}, ("task_id",))
def update_task(ctx: Ctx, task_id: int, status: str = "", note: str = "", assign_to: str = "") -> str:
    t = db.get_task(task_id)
    if not t or t["company_id"] != ctx.company["id"]:
        raise ToolError(f"Task #{task_id} not found.")
    fields: dict = {"updated_at": db.now()}
    if status:
        status = status.strip().lower()
        if status not in TASK_STATUSES:
            raise ToolError(f"Status must be one of {', '.join(TASK_STATUSES)}")
        fields["status"] = status
    if note:
        fields["result"] = ((t["result"] or "") + f"\n[{ctx.agent['name']}] {note}").strip()
    if assign_to:
        team = _team_roles(ctx)
        role = assign_to.strip().lower()
        if role not in team:
            raise ToolError(f"No teammate with role '{assign_to}'.")
        fields.update(role=role, agent_id=team[role]["id"])
    db.update("tasks", task_id, **fields)
    _log(ctx, "task", f"updated task #{task_id}{' → ' + status if status else ''}", task_id=task_id)
    bus.notify("tasks", company_id=ctx.company["id"])
    return f"Task #{task_id} updated."


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
    return "\n".join(f"#{t['id']} [{t['status']}] ({t['role']}) p{t['priority']} {t['title']}"
                     + (f" -> {t['deliverable']}" if t.get("deliverable") else "")
                     + (f" | {t['result'][-160:]}" if t["result"] else "") for t in rows)


# ======================= the shared ledger =======================

def _ledger_saved(ctx: Ctx, book: str) -> None:
    """After a ledger write: refresh the book's file in the company folder and tell the dashboard."""
    try:
        ctx.written.add(ledger.render(ctx.company, book))
    except Exception:
        pass
    bus.notify("ledger", company_id=ctx.company["id"], book=book)


def _ledger_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ledger.LedgerError as e:
        raise ToolError(str(e)) from None


@tool("remember", "Save a fact, decision, lesson or note to the shared ledger, where every teammate can find it.",
      {"content": S("What to remember (one clear statement)"),
       "kind": S("fact, decision, lesson, contact or note"),
       "pin": {"type": "boolean", "description": "Pin it so every agent always sees it (use sparingly)"}},
      ("content",))
def remember(ctx: Ctx, content: str, kind: str = "note", pin: bool = False) -> str:
    kind = (kind or "note").strip().lower()
    if kind == "decision":
        title = re.split(r"[.:;\n]", content.strip(), maxsplit=1)[0][:120]
        e = _ledger_call(ledger.decision, ctx.company["id"], title, content, by=ctx.agent["name"], agent_id=ctx.agent["id"])
        book = "decisions"
    else:
        e = _ledger_call(ledger.note, ctx.company["id"], content, kind, pin, ctx.agent["id"])
        book = "notes"
    _ledger_saved(ctx, book)
    _log(ctx, "memory", f"noted: {content[:140]}")
    return f"Saved to the ledger ({book} #{e['id']})."


@tool("recall", "Search the shared ledger, or list one of its books: notes, facts, decisions, contacts, money, metrics or handoffs.",
      {"query": S("Words to look for (leave empty to list a whole book)"),
       "book": S("notes, facts, decisions, contacts, money, metrics or handoffs; empty = everything"),
       "key": S("Optional: a fact key, metric name, contact email or task number to see its full history")}, ())
def recall(ctx: Ctx, query: str = "", book: str = "", key: str = "") -> str:
    return _ledger_call(ledger.text, ctx.company["id"], book, query, key)


@tool("record_fact", "Save a durable fact as key = value in the ledger (prices, URLs, accounts, policies, market numbers). "
                     "Writing the same key again replaces the value; the history is kept.",
      {"key": S("Short dotted key, e.g. pricing.pro_usd, site.url, policy.refund_days, market.size_usd"),
       "value": S("The value"), "source": S("Where it came from: a URL, a file, or 'owner'")}, ("key", "value"))
def record_fact(ctx: Ctx, key: str, value: str, source: str = "") -> str:
    e = _ledger_call(ledger.fact, ctx.company["id"], key, value, source, ctx.agent["id"])
    _ledger_saved(ctx, "facts")
    _log(ctx, "ledger", f"recorded fact {e['data']['key']} = {str(e['data']['value'])[:80]}")
    return {"created": "Saved", "updated": "Updated", "unchanged": "Already recorded"}[e["action"]] + f": {e['summary'][:200]}"


@tool("record_decision", "Record a decision in the ledger: what was decided, why, and what else was considered.",
      {"title": S("Short name for the decision, e.g. 'Pricing: single $29 plan'"),
       "decision": S("What was decided, in one or two sentences"), "why": S("The reasoning and evidence"),
       "options": S("Other options considered and why they lost"),
       "reversible": {"type": "boolean", "description": "Can it be undone cheaply? (default true)"}}, ("title", "decision"))
def record_decision(ctx: Ctx, title: str, decision: str, why: str = "", options: str = "", reversible: bool = True) -> str:
    e = _ledger_call(ledger.decision, ctx.company["id"], title, decision, why, options, reversible, ctx.agent["name"], ctx.agent["id"])
    _ledger_saved(ctx, "decisions")
    _log(ctx, "ledger", f"recorded decision: {e['data']['title']}")
    return f"Recorded decision #{e['id']}: {e['data']['title']}."


@tool("save_contact", "Add or update a lead, customer or partner in the ledger's contacts book (one entry per email address).",
      {"email": S("Email address (the identity of the contact)"), "name": S("Full name"), "company": S("Their company"),
       "role": S("Their job title"),
       "stage": S("new, contacted, replied, qualified, meeting, proposal, customer, churned, lost or do_not_contact"),
       "source": S("Where you found them (URL) or how they reached us"), "notes": S("One line of context: trigger, objection, next step")},
      ("email",))
def save_contact(ctx: Ctx, email: str, name: str = "", company: str = "", role: str = "", stage: str = "", source: str = "",
                 notes: str = "") -> str:
    e = _ledger_call(ledger.contact, ctx.company["id"], email, name, company, role, stage, source, notes, agent_id=ctx.agent["id"])
    _ledger_saved(ctx, "contacts")
    _log(ctx, "ledger", f"{'added' if e['action'] == 'created' else 'updated'} contact {e['data'].get('email') or e['data'].get('name')}")
    return {"created": "Added", "updated": "Updated", "unchanged": "Already up to date"}[e["action"]] + f": {e['summary'][:240]}"


@tool("record_transaction", "Record money in or out in the ledger's money book, one entry per transaction.",
      {"date": S("YYYY-MM-DD"), "amount": {"type": "number", "description": "Positive amount"},
       "currency": S("USD, EUR, GBP... (default USD)"), "direction": S("in (money received) or out (money paid)"),
       "category": S(", ".join(ledger.CATEGORIES)), "counterparty": S("Who paid or was paid"),
       "description": S("What it was for"), "external_id": S("Id from the payment provider, bank or invoice, if any")},
      ("date", "amount", "category"))
def record_transaction(ctx: Ctx, date: str, amount: float, category: str, currency: str = "USD", direction: str = "",
                       counterparty: str = "", description: str = "", external_id: str = "") -> str:
    e = _ledger_call(ledger.transaction, ctx.company["id"], date, amount, currency, direction, category, counterparty,
                     description, external_id, "", ctx.agent["id"])
    _ledger_saved(ctx, "transactions")
    _log(ctx, "ledger", f"recorded {e['summary'][:120]}")
    if e["action"] == "unchanged":
        return f"That transaction is already in the ledger (#{e['id']}). Not recorded twice."
    return f"Recorded in the money book (#{e['id']}): {e['summary'][:200]}"


@tool("record_metric", "Record a KPI reading in the ledger (e.g. visitors_7d, signups, revenue_usd, emails_sent, leads).",
      {"name": S("Metric name, snake_case"), "value": {"type": "number", "description": "The value"},
       "note": S("Where the number came from")}, ("name", "value"))
def record_metric(ctx: Ctx, name: str, value: float, note: str = "") -> str:
    e = _ledger_call(ledger.metric, ctx.company["id"], name, value, note, agent_id=ctx.agent["id"])
    _ledger_saved(ctx, "metrics")
    _log(ctx, "metric", f"recorded {e['data']['name']} = {value:g}")
    return f"Recorded {e['data']['name']} = {value:g}."


# ======================= connectors =======================

def _cs(ctx: Ctx) -> dict:
    return db.company_settings(db.get_company(ctx.company["id"]) or ctx.company)


def _connector_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except connectors.ConnectorError as e:
        raise ToolError(str(e)) from None


def _cur_name(name: str, currency: str) -> str:
    return f"{name}_{(currency or 'usd').lower()}"


@tool("payments_summary", "Balance, sales, refunds, fees, active subscriptions and MRR from the connected payment provider "
                          "(Stripe, Lemon Squeezy or Gumroad). Also records the mrr and balance metrics.",
      {"days": I("Look back this many days (default 30)")})
def payments_summary(ctx: Ctx, days: int = 30) -> str:
    days = max(1, min(int(days or 30), 365))
    s = _connector_call(connectors.payments_summary, _cs(ctx), days)
    _log(ctx, "tool", f"checked payments ({s['provider']}, last {days} days)")
    cid, aid = ctx.company["id"], ctx.agent["id"]
    if s.get("mrr") is not None:
        ledger.metric(cid, _cur_name("mrr", s["currency"]), round(s["mrr"], 2), f"{s['provider']}: {s['subscriptions_active']} active subscriptions", agent_id=aid)
    if s.get("balance_available") is not None:
        ledger.metric(cid, _cur_name("balance", s["currency"]), round(s["balance_available"], 2), f"{s['provider']} available balance", agent_id=aid)
    _ledger_saved(ctx, "metrics")
    return connectors.payments_text(s) + "\nUse sync_payments to bring these into the ledger's money book."


@tool("sync_payments", "Pull charges, refunds and fees from the connected payment provider into the ledger's money book. "
                       "Safe to run repeatedly: nothing is recorded twice.",
      {"days": I("Look back this many days (default 30)")})
def sync_payments(ctx: Ctx, days: int = 30) -> str:
    days = max(1, min(int(days or 30), 365))
    cs = _cs(ctx)
    rows = _connector_call(connectors.payments_transactions, cs, days)
    provider = connectors.payments_provider(cs) or "payments"
    cid, aid = ctx.company["id"], ctx.agent["id"]
    created = 0
    for r in rows:
        e = ledger.transaction(cid, r["date"], r["amount"], r["currency"], r["direction"], r["category"], r["counterparty"],
                               r["description"], r["external_id"], r["provider"], aid, source=r["provider"])
        created += e["action"] != "unchanged"
    cur = rows[0]["currency"] if rows else "USD"
    revenue = sum(r["amount"] for r in rows if r["category"] == "revenue")
    refunds = sum(r["amount"] for r in rows if r["category"] == "refunds" and r["direction"] == "out")
    fees = sum(r["amount"] for r in rows if r["category"] == "fees" and r["direction"] == "out")
    ledger.metric(cid, _cur_name(f"revenue_{days}d", cur), round(revenue, 2), f"{provider} gross sales, last {days} days", agent_id=aid)
    _ledger_saved(ctx, "transactions")
    _ledger_saved(ctx, "metrics")
    _log(ctx, "ledger", f"synced {len(rows)} payment entries from {provider} ({created} new)")
    return (f"Synced {len(rows)} entries from {provider}: {created} new, {len(rows) - created} already in the ledger. Last {days} "
            f"days: revenue {revenue:,.2f} {cur}, refunds {refunds:,.2f} {cur}, fees {fees:,.2f} {cur}. "
            f"See the money book with recall(book='money') or ledger/transactions.csv.")


@tool("site_stats", "Visitors, visits, pageviews, top pages and top sources from the connected analytics (Plausible or "
                    "GoatCounter). Also records the visitors and pageviews metrics.",
      {"period": S("7d (default), 30d, month, 6mo or 12mo")})
def site_stats(ctx: Ctx, period: str = "7d") -> str:
    period = (period or "7d").strip().lower()
    s = _connector_call(connectors.site_stats, _cs(ctx), period)
    _log(ctx, "tool", f"checked site analytics ({s['provider']}, {s['period']})")
    cid, aid = ctx.company["id"], ctx.agent["id"]
    ledger.metric(cid, f"visitors_{s['period']}", s["visitors"], f"{s['provider']}", agent_id=aid)
    ledger.metric(cid, f"pageviews_{s['period']}", s["pageviews"], f"{s['provider']}", agent_id=aid)
    _ledger_saved(ctx, "metrics")
    return connectors.stats_text(s)


@tool("list_signups", "Recent form submissions from the website (waitlist, contact form) via Netlify Forms. New ones are "
                      "added to the ledger's contacts book.",
      {"limit": I("How many to fetch (default 50, max 100)")})
def list_signups(ctx: Ctx, limit: int = 50) -> str:
    limit = max(1, min(int(limit or 50), 100))
    subs = _connector_call(connectors.netlify_submissions, _cs(ctx), limit)
    cid, aid = ctx.company["id"], ctx.agent["id"]
    added = 0
    for s in subs:
        if not s["email"]:
            continue
        extra = "; ".join(f"{k}: {v[:80]}" for k, v in list(s["fields"].items())[:4])
        try:
            e = ledger.contact(cid, s["email"], s["name"], source_note=f"website form '{s['form']}'", notes=extra, agent_id=aid,
                               source="netlify")
        except ledger.LedgerError:
            continue
        added += e["action"] == "created"
    if len(subs) < limit:
        ledger.metric(cid, "signups_total", len(subs), "Netlify form submissions", agent_id=aid)
    _ledger_saved(ctx, "contacts")
    _ledger_saved(ctx, "metrics")
    _log(ctx, "tool", f"read {len(subs)} form submissions ({added} new contacts)")
    if not subs:
        return "No form submissions yet."
    lines = [f"{len(subs)} submissions ({added} new contacts added to the ledger). Newest first:"]
    for s in subs[:25]:
        bits = ", ".join(f"{k}: {v[:60]}" for k, v in list(s["fields"].items())[:3])
        lines.append(f"- {s['created_at'][:10]} {s['name'] or ''} {s['email'] or '(no email)'} [{s['form']}]" + (f" {bits}" if bits else ""))
    return "\n".join(lines)


@tool("check_domain", "Check whether a domain name is already registered (public RDAP data; no account needed).",
      {"domain": S("A domain name like pawplan.com")}, ("domain",))
def check_domain(ctx: Ctx, domain: str) -> str:
    r = _connector_call(connectors.domain_check, domain)
    _log(ctx, "tool", f"checked domain {r['domain']}")
    if r["registered"] is False:
        return f"{r['domain']}: not registered. {r['note']}"
    if r["registered"] is None:
        return f"{r['domain']}: unknown. {r['note']}"
    return (f"{r['domain']}: registered" + (f" via {r['registrar']}" if r.get("registrar") else "")
            + (f" since {r['registered_on']}" if r.get("registered_on") else "") + (f", expires {r['expires']}" if r.get("expires") else "")
            + ". Pick another name or a different ending.")


@tool("list_bookings", "Upcoming meetings booked through the connected scheduling tool (Cal.com).",
      {"limit": I("How many (default 20)")})
def list_bookings(ctx: Ctx, limit: int = 20) -> str:
    rows = _connector_call(connectors.calcom_bookings, _cs(ctx), limit)
    _log(ctx, "tool", f"checked upcoming bookings ({len(rows)})")
    if not rows:
        return "No upcoming bookings."
    return "\n".join(f"- {b['start'][:16].replace('T', ' ')} {b['title']} with {', '.join(b['attendees']) or '?'}"
                      + (f" ({b['location']})" if b["location"] else "") for b in rows)


# ======================= owner =======================

@tool("report_to_owner", "Send the owner a message in the dashboard chat. Use for meaningful updates or when they asked you something.",
      {"message": S("The message (concise, Markdown allowed)")}, ("message",))
def report_to_owner(ctx: Ctx, message: str) -> str:
    db.run("INSERT INTO messages(company_id, agent_id, sender, content, created_at) VALUES(?,?,?,?,?)",
           (ctx.company["id"], ctx.agent["id"], "agent", message, db.now()))
    _log(ctx, "message", f"messaged the owner: {message[:160]}")
    bus.notify("messages", company_id=ctx.company["id"])
    if ctx.kind != "chat":
        connectors.notify("messages", f"{ctx.agent['name']} at {ctx.company['name']}", message)
    if ctx.kind == "chat":
        ctx.reply = message
        ctx.reply_sent = True
        ctx.finished = True
    return "Delivered to the owner."


@tool("ask_owner", "Ask the owner a question only they can answer (a decision, an account, a preference).",
      {"question": S("The question, with the options you see")}, ("question",))
def ask_owner(ctx: Ctx, question: str) -> str:
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "question", {"question": question})


# ======================= outbound =======================

def _emails_today(cid: int) -> int:
    sent = db.scalar("SELECT COUNT(*) FROM emails WHERE company_id=? AND direction='out' AND created_at >= ?",
                     (cid, db.now()[:10]))
    pending = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND action='send_email' AND status='pending'",
                        (cid,))
    return (sent or 0) + (pending or 0)


@tool("send_email", "Send an email from the company mailbox (may need the owner's approval).",
      {"to": S("Recipient email address"), "subject": S("Subject line"), "body": S("Plain-text body, signed with the company name"),
       "reply_to_email_id": I("If replying to an inbound email, its number")}, ("to", "subject", "body"))
def send_email(ctx: Ctx, to: str, subject: str, body: str, reply_to_email_id: int = 0) -> str:
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to.strip()):
        raise ToolError("That doesn't look like a valid email address.")
    limit = int(db.company_settings(ctx.company).get("daily_email_limit", 25))
    if _emails_today(ctx.company["id"]) >= limit:
        return f"Daily email limit ({limit}) reached. Try again tomorrow."
    payload = {"to": to.strip(), "subject": subject, "body": body}
    if reply_to_email_id:
        payload["reply_to_email_id"] = int(reply_to_email_id)
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
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "social_post",
                        {"platform": platform.lower(), "content": content, "link": link})


@tool("deploy_site", "Publish the website in site/ to the internet (may need the owner's approval).", {})
def deploy_site(ctx: Ctx) -> str:
    if not workspace.safe_path(ctx.company, "site/index.html").exists():
        raise ToolError("Create site/index.html first.")
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "deploy_site", {"folder": "site"})


@tool("http_request", "Call an outside web API. GET runs directly; POST/PUT/PATCH/DELETE may need approval.",
      {"url": S("Full URL"), "method": S("GET, POST, PUT, PATCH or DELETE"), "body": S("JSON body as a string"),
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


@tool("run_code", "Run a Python script or shell command inside the company folder (may need approval).",
      {"language": S("python or shell"), "code": S("The code to run"), "timeout": I("Seconds, max 600 (default 60)")},
      ("code",))
def run_code(ctx: Ctx, code: str, language: str = "python", timeout: int = 60) -> str:
    lang = "python" if (language or "python").lower().startswith("py") else "shell"
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "run_code",
                        {"language": lang, "code": code, "timeout": int(timeout or 60)})


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
       "stage": S("idea, building, launched or revenue")}, ("charter",))
def update_charter(ctx: Ctx, charter: str, company_name: str = "", one_liner: str = "", stage: str = "") -> str:
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
       "kind": S("landing, cold_email, email, support_reply, social, social_x, article or general")}, ())
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


@tool("finish", "End your work session with a handoff for the team.",
      {"summary": S("What you did, in up to 5 short bullets"),
       "status": S("done, partial (some progress, more needed), blocked (can't proceed) or waiting (on the owner)"),
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
    if status == "done" and ctx.task:
        problem = _deliverable_ok(ctx, target)
        if problem and not ctx.finish_warned:
            ctx.finish_warned = True
            return (f"Not finished yet: {problem}. Create or update it, then call finish again. If you can't, call "
                    f"finish with status 'partial' and explain what's missing.")
        if problem:
            status = "partial"
            open_issues = (open_issues + f"\nDeliverable check failed: {problem}.").strip()
    parts = [f"Status: {status}"]
    if target:
        parts.append(f"Deliverable: {target}")
    parts.append(f"Summary: {summary.strip()}")
    for label, val in (("Evidence", evidence), ("Open issues", open_issues), ("Suggested next", next_step)):
        if val and val.strip():
            parts.append(f"{label}: {val.strip()}")
    ctx.finished = True
    ctx.finish_status = status
    ctx.finish_summary = "\n".join(parts)
    ctx.deliverable = target
    ctx.finish_fields = {"summary": summary.strip(), "deliverable": target, "evidence": evidence, "open_issues": open_issues,
                         "next_step": next_step}
    return "Session ended."


def role_tools(agent: dict, kind: str) -> list[dict]:
    return specs(roles.tools_for(agent, kind))
