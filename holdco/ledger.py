"""The shared ledger: one append-only journal that every agent reads and writes. The team's source of truth.

Why a ledger instead of free-form "memory":
- Agents in a multi-agent company mostly fail by losing state: one agent decides a price, another quotes a
  different one; a lead is emailed twice; a number in a report can't be traced. A ledger fixes that by giving
  every durable piece of information one place, one owner-visible history and one way to look it up.
- It is append-only. Entries are never edited or deleted, only superseded (same key, newer entry) or voided
  (struck through). "What do we believe now?" is the current entry for a key; "how did we get here?" is the
  chain behind it. That is how accounting ledgers earn trust, and it is what owners need to audit agents.
- Entries are typed into books so they can be checked, deduplicated and summarised instead of re-read as prose:

    notes         free-form facts, lessons and context (what "memory" used to be)
    facts         key = value, e.g. pricing.pro_usd = 29  (the latest entry per key wins; history kept)
    decisions     what was decided, why, the options considered, whether it is reversible, who decided
    contacts      leads, customers and partners, deduplicated by email, with a pipeline stage
    transactions  money in and out, categorised with the chart of accounts, deduplicated by provider id
    metrics       KPI readings over time (the latest per name is "the number"; the rest is the trend)
    handoffs      the structured end-of-task report of every finished task (status, deliverable, evidence...)

Every book is also rendered to plain files in the company folder (ledger/*.md and *.csv) so humans and
file-based tools can read it, and can be exported as CSV for an accountant or a CRM.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
import threading
from typing import Any

from . import db

BOOKS: dict[str, dict] = {
    "notes": {"label": "Notes", "keyed": False, "file": "ledger/notes.md"},
    "facts": {"label": "Facts", "keyed": True, "file": "ledger/facts.md"},
    "decisions": {"label": "Decisions", "keyed": True, "file": "ledger/decisions.md"},
    "contacts": {"label": "Contacts", "keyed": True, "file": "ledger/contacts.csv"},
    "transactions": {"label": "Money", "keyed": True, "file": "ledger/transactions.csv"},
    "metrics": {"label": "Metrics", "keyed": True, "file": "ledger/metrics.csv"},
    "handoffs": {"label": "Handoffs", "keyed": True, "file": "ledger/handoffs.md"},
}
BOOK_ALIASES = {"money": "transactions", "finance": "transactions", "finances": "transactions", "cash": "transactions",
                "ledger": "transactions", "transaction": "transactions", "people": "contacts", "leads": "contacts",
                "customers": "contacts", "crm": "contacts", "contact": "contacts", "kpis": "metrics", "kpi": "metrics",
                "numbers": "metrics", "metric": "metrics", "tasks": "handoffs", "handoff": "handoffs",
                "lessons": "notes", "memory": "notes", "note": "notes", "fact": "facts", "decision": "decisions"}

# Chart of accounts: category -> (label, default direction)
CATEGORIES: dict[str, tuple[str, str]] = {
    "revenue": ("Revenue", "in"),
    "refunds": ("Refunds and chargebacks", "out"),
    "fees": ("Payment processing fees", "out"),
    "cost_of_revenue": ("Cost of revenue (hosting, AI and API costs)", "out"),
    "advertising": ("Advertising", "out"),
    "contractors": ("Contractors", "out"),
    "software": ("Software and subscriptions", "out"),
    "legal": ("Legal and professional", "out"),
    "taxes": ("Taxes and licences", "out"),
    "sales_tax": ("Sales tax and VAT", "out"),
    "owner_contribution": ("Owner contribution", "in"),
    "owner_draw": ("Owner draw", "out"),
    "other": ("Other", "out"),
}
_CATEGORY_HINTS = [
    ("sales_tax", ("sales tax", "vat", "gst")), ("refunds", ("refund", "chargeback", "dispute")),
    ("fees", ("fee", "stripe fee", "processing")), ("revenue", ("revenue", "sale", "subscription", "income", "payment", "order")),
    ("advertising", ("ad", "ads", "advert", "campaign", "meta", "google ads")),
    ("contractors", ("contractor", "freelance", "upwork", "fiverr", "designer")),
    ("software", ("software", "saas", "tool", "license", "licence", "domain", "hosting plan")),
    ("cost_of_revenue", ("cost of revenue", "cogs", "hosting", "server", "api", "ai", "openai", "compute", "email service")),
    ("legal", ("legal", "lawyer", "accountant", "professional", "bookkeep")),
    ("taxes", ("tax", "licen", "registration", "filing")),
    ("owner_contribution", ("contribution", "owner invest", "capital", "deposit from owner")),
    ("owner_draw", ("draw", "distribution", "owner withdraw")),
]
STAGES = ("new", "contacted", "replied", "qualified", "meeting", "proposal", "customer", "churned", "lost", "do_not_contact")
STAGE_ALIASES = {"lead": "new", "prospect": "new", "researched": "new", "found": "new", "emailed": "contacted", "reached_out": "contacted",
                 "followed_up": "contacted", "responded": "replied", "interested": "replied", "engaged": "replied", "demo": "meeting",
                 "call": "meeting", "booked": "meeting", "quote": "proposal", "quoted": "proposal", "negotiating": "proposal",
                 "won": "customer", "closed_won": "customer", "paying": "customer", "client": "customer", "active": "customer",
                 "cancelled": "churned", "canceled": "churned", "churn": "churned", "closed_lost": "lost", "declined": "lost",
                 "not_interested": "lost", "unsubscribed": "do_not_contact", "opt_out": "do_not_contact", "optout": "do_not_contact",
                 "opted_out": "do_not_contact", "dnc": "do_not_contact", "suppressed": "do_not_contact", "bounced": "do_not_contact"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, agent_id INTEGER, book TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
    pinned INTEGER DEFAULT 0, supersedes INTEGER, current INTEGER DEFAULT 1, voided INTEGER DEFAULT 0,
    source TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_book ON ledger(company_id, book, current, id);
CREATE INDEX IF NOT EXISTS ix_ledger_key ON ledger(company_id, book, key);
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS ledger_fts USING fts5(summary, content='ledger', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS ledger_ai AFTER INSERT ON ledger BEGIN
  INSERT INTO ledger_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS ledger_ad AFTER DELETE ON ledger BEGIN
  INSERT INTO ledger_fts(ledger_fts, rowid, summary) VALUES('delete', old.id, old.summary);
END;
"""

_render_lock = threading.Lock()
HAS_FTS = False


class LedgerError(ValueError):
    pass


def init() -> None:
    """Create the tables and bring data from the old memory/metrics tables into the ledger (once)."""
    global HAS_FTS
    with db.locked():
        db.executescript(SCHEMA)
        try:
            db.executescript(FTS_SCHEMA)
            HAS_FTS = True
        except Exception:
            HAS_FTS = False
        _migrate_legacy()


def _migrate_legacy() -> None:
    if db.scalar("SELECT COUNT(*) FROM ledger"):
        return
    rows = db.q("SELECT * FROM memory ORDER BY id")
    for m in rows:
        data = {"kind": m.get("kind") or "note", "content": m["content"] or ""}
        db.run("INSERT INTO ledger(company_id, agent_id, book, key, summary, data, pinned, source, created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (m["company_id"], m["agent_id"], "notes", "", (m["content"] or "")[:4000], json.dumps(data),
                int(bool(m.get("pinned"))), "owner" if m["agent_id"] is None else "agent", m["created_at"] or db.now()))
    latest: dict[tuple, int] = {}
    for r in db.q("SELECT * FROM metrics ORDER BY id"):
        data = {"name": r["name"], "value": r["value"], "note": r.get("note") or "", "period": ""}
        prev = latest.get((r["company_id"], r["name"]))
        lid = db.run("INSERT INTO ledger(company_id, agent_id, book, key, summary, data, supersedes, source, created_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     (r["company_id"], r["agent_id"], "metrics", r["name"], _metric_summary(r["name"], r["value"], r.get("note") or ""),
                      json.dumps(data), prev, "agent", r["created_at"] or db.now()))
        if prev:
            db.run("UPDATE ledger SET current=0 WHERE id=?", (prev,))
        latest[(r["company_id"], r["name"])] = lid


# ---------------------------------------------------------------- core

def book_name(name: str) -> str:
    n = (name or "").strip().lower()
    n = BOOK_ALIASES.get(n, n)
    if n not in BOOKS:
        raise LedgerError(f"Unknown book '{name}'. Books: {', '.join(BOOKS)}")
    return n


def _row(r: dict) -> dict:
    r = dict(r)
    r["data"] = db.loads(r.get("data"), {}) or {}
    return r


def add(company_id: int, book: str, data: dict, summary: str, key: str = "", agent_id: int | None = None,
        source: str = "agent", pinned: bool = False, created_at: str | None = None) -> dict:
    """Append an entry. For keyed books the new entry supersedes the current one with the same key.
    Returns the entry plus `action`: created, updated or unchanged (identical to the current entry)."""
    book = book_name(book)
    key = (key or "").strip()[:200]
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    summary = " ".join((summary or "").split())[:4000]
    with db.locked():
        prev = None
        if key:
            prev = db.one("SELECT * FROM ledger WHERE company_id=? AND book=? AND key=? AND current=1 AND voided=0 "
                          "ORDER BY id DESC LIMIT 1", (company_id, book, key))
            if prev and json.dumps(db.loads(prev["data"], {}), sort_keys=True, ensure_ascii=False, default=str) == blob \
                    and bool(prev["pinned"]) == bool(pinned):
                out = _row(prev)
                out["action"] = "unchanged"
                return out
        lid = db.run("INSERT INTO ledger(company_id, agent_id, book, key, summary, data, pinned, supersedes, source, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (company_id, agent_id, book, key, summary, blob, int(bool(pinned)), prev["id"] if prev else None,
                      source[:40], created_at or db.now()))
        if prev:
            db.run("UPDATE ledger SET current=0 WHERE id=?", (prev["id"],))
    out = _row(db.one("SELECT * FROM ledger WHERE id=?", (lid,)))
    out["action"] = "updated" if prev else "created"
    return out


def get(entry_id: int) -> dict | None:
    r = db.one("SELECT * FROM ledger WHERE id=?", (entry_id,))
    return _row(r) if r else None


def current(company_id: int, book: str, key: str | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
    book = book_name(book)
    sql = "SELECT * FROM ledger WHERE company_id=? AND book=? AND current=1 AND voided=0"
    params: list = [company_id, book]
    if key is not None:
        sql += " AND key=?"
        params.append(key)
    sql += f" ORDER BY pinned DESC, id DESC LIMIT {int(limit)} OFFSET {int(offset)}"
    return [_row(r) for r in db.q(sql, params)]


def history(company_id: int, book: str, key: str, limit: int = 100, include_voided: bool = False) -> list[dict]:
    book = book_name(book)
    cond = "" if include_voided else " AND voided=0"
    return [_row(r) for r in db.q(f"SELECT * FROM ledger WHERE company_id=? AND book=? AND key=?{cond} "
                                  f"ORDER BY id DESC LIMIT {int(limit)}", (company_id, book, key))]


def all_entries(company_id: int, book: str = "", limit: int = 500, include_voided: bool = False) -> list[dict]:
    sql = "SELECT * FROM ledger WHERE company_id=?"
    params: list = [company_id]
    if book:
        sql += " AND book=?"
        params.append(book_name(book))
    if not include_voided:
        sql += " AND voided=0"
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    return [_row(r) for r in db.q(sql, params)]


def search(company_id: int, query: str, book: str = "", limit: int = 20, current_only: bool = True) -> list[dict]:
    """Full-text search over summaries (FTS5 when available, LIKE otherwise)."""
    bk = book_name(book) if book else ""
    cond = " AND l.book=?" if bk else ""
    cur = " AND l.current=1" if current_only else ""
    rows: list[dict] = []
    words = re.findall(r"\w+", query or "")[:8]
    if HAS_FTS and words:
        terms = " OR ".join(f'"{w}"' for w in words)
        try:
            rows = db.q(f"SELECT l.* FROM ledger_fts f JOIN ledger l ON l.id=f.rowid WHERE ledger_fts MATCH ? "
                        f"AND l.company_id=? AND l.voided=0{cur}{cond} ORDER BY rank LIMIT {int(limit)}",
                        [terms, company_id] + ([bk] if bk else []))
        except Exception:
            rows = []
    if not rows and words:
        like = " AND ".join("l.summary LIKE ?" for _ in words[:4])
        rows = db.q(f"SELECT l.* FROM ledger l WHERE l.company_id=? AND l.voided=0{cur}{cond} AND {like} "
                    f"ORDER BY l.id DESC LIMIT {int(limit)}",
                    [company_id] + ([bk] if bk else []) + [f"%{w}%" for w in words[:4]])
    return [_row(r) for r in rows]


def void(entry_id: int, by: str = "owner") -> dict | None:
    """Strike an entry out. It stays in the history; it just stops being current."""
    r = db.one("SELECT * FROM ledger WHERE id=?", (entry_id,))
    if not r:
        return None
    db.update("ledger", entry_id, voided=1, current=0)
    return _row(db.one("SELECT * FROM ledger WHERE id=?", (entry_id,)))


def set_pinned(entry_id: int, pinned: bool) -> None:
    db.update("ledger", entry_id, pinned=1 if pinned else 0)


def count(company_id: int, book: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM ledger WHERE company_id=? AND book=? AND current=1 AND voided=0",
                     (company_id, book_name(book))) or 0


def delete_company(company_id: int) -> None:
    db.run("DELETE FROM ledger WHERE company_id=?", (company_id,))


# ---------------------------------------------------------------- typed writers

def _by(agent_id: int | None) -> str:
    if agent_id is None:
        return "owner"
    a = db.get_agent(agent_id)
    return a["name"] if a else "agent"


def note(company_id: int, content: str, kind: str = "note", pinned: bool = False, agent_id: int | None = None,
         source: str = "agent") -> dict:
    content = (content or "").strip()[:4000]
    if not content:
        raise LedgerError("Write something to remember.")
    kind = re.sub(r"[^a-z_]", "", (kind or "note").lower())[:20] or "note"
    return add(company_id, "notes", {"kind": kind, "content": content}, content, "", agent_id, source, pinned)


def fact(company_id: int, key: str, value: Any, source_note: str = "", agent_id: int | None = None,
         source: str = "agent") -> dict:
    k = re.sub(r"[^a-z0-9_.\-/]+", "_", str(key or "").strip().lower()).strip("_.")[:120]
    if not k:
        raise LedgerError("Give the fact a short key like pricing.pro_usd or site.url.")
    v = value if isinstance(value, (int, float, bool)) else str(value or "").strip()[:2000]
    if v == "":
        raise LedgerError("Give the fact a value.")
    data = {"key": k, "value": v, "source": (source_note or "").strip()[:300]}
    summary = f"{k} = {v}" + (f" (source: {data['source']})" if data["source"] else "")
    return add(company_id, "facts", data, summary, k, agent_id, source)


def decision(company_id: int, title: str, decided: str, why: str = "", options: str = "", reversible: bool = True,
             by: str = "", agent_id: int | None = None, source: str = "agent", status: str = "active") -> dict:
    title = " ".join((title or "").split())[:160]
    decided = (decided or "").strip()[:3000]
    if not title or not decided:
        raise LedgerError("A decision needs a title and the decision itself.")
    key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
    data = {"title": title, "decision": decided, "why": (why or "").strip()[:2000], "options": (options or "").strip()[:2000],
            "reversible": bool(reversible), "by": by or _by(agent_id), "status": status}
    summary = f"{title}: {decided}" + (f" Why: {data['why']}" if data["why"] else "")
    return add(company_id, "decisions", data, summary, key, agent_id, source)


def contact(company_id: int, email: str = "", name: str = "", company: str = "", role: str = "", stage: str = "",
            source_note: str = "", notes: str = "", do_not_contact: bool | None = None, url: str = "",
            agent_id: int | None = None, source: str = "agent") -> dict:
    email = (email or "").strip().lower()[:200]
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise LedgerError("That doesn't look like a valid email address.")
    name = " ".join((name or "").split())[:120]
    company = " ".join((company or "").split())[:120]
    if not email and not (name or company):
        raise LedgerError("Give at least an email, or a name and company.")
    key = email or re.sub(r"[^a-z0-9]+", "-", f"{name} {company}".lower()).strip("-")[:120]
    stage = re.sub(r"[^a-z_]", "", (stage or "").lower().replace("-", "_").replace(" ", "_"))
    stage = STAGE_ALIASES.get(stage, stage)
    if stage and stage not in STAGES:
        near = next((s for s in STAGES if s.startswith(stage[:4])), None)
        if not near:
            raise LedgerError(f"Stage must be one of {', '.join(STAGES)}")
        stage = near
    prev = current(company_id, "contacts", key, limit=1)
    old = prev[0]["data"] if prev else {}
    data = {
        "email": email or old.get("email", ""), "name": name or old.get("name", ""), "company": company or old.get("company", ""),
        "role": (role or "").strip()[:120] or old.get("role", ""), "stage": stage or old.get("stage", "new"),
        "source": (source_note or "").strip()[:300] or old.get("source", ""),
        "notes": (notes or "").strip()[:2000] or old.get("notes", ""), "url": (url or "").strip()[:300] or old.get("url", ""),
        "do_not_contact": bool(do_not_contact) if do_not_contact is not None else bool(old.get("do_not_contact", False)),
        "first_seen": old.get("first_seen") or db.now()[:10], "touches": int(old.get("touches", 0) or 0),
    }
    if data["stage"] == "do_not_contact":
        data["do_not_contact"] = True
    if stage == "contacted" and (not prev or old.get("stage") != "contacted" or notes):
        data["touches"] = data["touches"] + 1
    who = data["name"] or data["email"] or data["company"]
    summary = (f"{who}" + (f" <{data['email']}>" if data["email"] and data["name"] else "")
               + (f" at {data['company']}" if data["company"] and data["company"] != who else "")
               + (f", {data['role']}" if data["role"] else "") + f" — {data['stage']}"
               + (" (do not contact)" if data["do_not_contact"] else "")
               + (f". {data['notes']}" if data["notes"] else "") + (f" [source: {data['source']}]" if data["source"] else ""))
    return add(company_id, "contacts", data, summary, key, agent_id, source)


def normalize_category(category: str) -> str:
    c = re.sub(r"[^a-z_ ]", "", (category or "").lower().replace("-", "_")).strip()
    if c.replace(" ", "_") in CATEGORIES:
        return c.replace(" ", "_")
    for key, hints in _CATEGORY_HINTS:
        if any(h in c for h in hints):
            return key
    return "other"


def transaction(company_id: int, date: str, amount: float, currency: str = "USD", direction: str = "",
                category: str = "other", counterparty: str = "", description: str = "", external_id: str = "",
                provider: str = "", agent_id: int | None = None, source: str = "agent") -> dict:
    date = (date or "").strip()[:10]
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise LedgerError("date must be YYYY-MM-DD") from None
    try:
        amount = round(abs(float(amount)), 2)
    except (TypeError, ValueError):
        raise LedgerError("amount must be a number") from None
    if amount == 0:
        raise LedgerError("amount can't be zero")
    cat = normalize_category(category)
    direction = (direction or "").strip().lower()
    direction = {"income": "in", "inbound": "in", "credit": "in", "received": "in", "expense": "out", "outbound": "out",
                 "debit": "out", "paid": "out", "spent": "out"}.get(direction, direction)
    if direction not in ("in", "out"):
        direction = CATEGORIES[cat][1]
    currency = re.sub(r"[^A-Z]", "", (currency or "USD").upper())[:3] or "USD"
    external_id = (external_id or "").strip()[:120]
    provider = re.sub(r"[^a-z0-9_]", "", (provider or "").lower())[:30]
    data = {"date": date, "amount": amount, "currency": currency, "direction": direction, "category": cat,
            "counterparty": " ".join((counterparty or "").split())[:160], "description": " ".join((description or "").split())[:400],
            "external_id": external_id, "provider": provider}
    if external_id:
        key = f"{provider}:{external_id}" if provider else external_id
    else:  # manual entries: the same transaction recorded twice is one transaction
        key = "manual:" + hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
    sign = "+" if direction == "in" else "-"
    summary = (f"{date} {sign}{amount:.2f} {currency} {CATEGORIES[cat][0]}"
               + (f" {data['counterparty']}" if data["counterparty"] else "")
               + (f": {data['description']}" if data["description"] else "")
               + (f" [{provider or 'id'} {external_id}]" if external_id else ""))
    return add(company_id, "transactions", data, summary, key, agent_id, source)


def _metric_summary(name: str, value: float, note: str) -> str:
    try:
        v = f"{float(value):g}"
    except (TypeError, ValueError):
        v = str(value)
    return f"{name} = {v}" + (f" ({note})" if note else "")


def metric(company_id: int, name: str, value: float, note: str = "", period: str = "", agent_id: int | None = None,
           source: str = "agent") -> dict:
    name = re.sub(r"[^a-z0-9_]+", "_", str(name or "").lower()).strip("_")[:40]
    if not name:
        raise LedgerError("Give the metric a snake_case name.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise LedgerError("value must be a number") from None
    data = {"name": name, "value": value, "note": (note or "").strip()[:300], "period": (period or "").strip()[:20]}
    # A metric reading is a new entry even when the value is unchanged (it's a reading at a point in time).
    prevs = current(company_id, "metrics", name, limit=1)
    if prevs and prevs[0]["data"].get("value") == value and prevs[0]["data"].get("note") == data["note"] \
            and prevs[0]["created_at"][:10] == db.now()[:10]:
        out = prevs[0]
        out["action"] = "unchanged"
        return out
    return _force_add(company_id, "metrics", data, _metric_summary(name, value, data["note"]), name, agent_id, source)


def _force_add(company_id, book, data, summary, key, agent_id, source) -> dict:
    """Like add(), but always appends even if the data equals the current entry."""
    with db.locked():
        prev = db.one("SELECT id FROM ledger WHERE company_id=? AND book=? AND key=? AND current=1 AND voided=0", (company_id, book, key))
        lid = db.run("INSERT INTO ledger(company_id, agent_id, book, key, summary, data, supersedes, source, created_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     (company_id, agent_id, book, key, summary, json.dumps(data, sort_keys=True, default=str),
                      prev["id"] if prev else None, source[:40], db.now()))
        if prev:
            db.run("UPDATE ledger SET current=0 WHERE id=?", (prev["id"],))
    out = _row(db.one("SELECT * FROM ledger WHERE id=?", (lid,)))
    out["action"] = "updated" if prev else "created"
    return out


def handoff(company_id: int, task: dict, agent: dict, status: str, summary_text: str, deliverable: str = "",
            evidence: str = "", open_issues: str = "", next_step: str = "") -> dict:
    data = {"task_id": task["id"], "title": task.get("title", ""), "status": status, "deliverable": deliverable or "",
            "summary": (summary_text or "").strip()[:2000], "evidence": (evidence or "").strip()[:1000],
            "open_issues": (open_issues or "").strip()[:1000], "next_step": (next_step or "").strip()[:500],
            "agent": agent.get("name", ""), "role": agent.get("role", "")}
    first = (data["summary"].splitlines() or [""])[0]
    summary = f"Task #{task['id']} {status}: {data['title']} — {first}" + (f" (deliverable {deliverable})" if deliverable else "")
    return add(company_id, "handoffs", data, summary, f"task:{task['id']}", agent.get("id"), "agent")


# ---------------------------------------------------------------- reading

def latest_metrics(company_id: int) -> list[dict]:
    """One row per metric name, in the shape the dashboard and prompts expect."""
    out = []
    for e in current(company_id, "metrics", limit=200):
        d = e["data"]
        out.append({"id": e["id"], "company_id": company_id, "agent_id": e["agent_id"], "name": d.get("name", e["key"]),
                    "value": d.get("value", 0), "note": d.get("note", ""), "period": d.get("period", ""),
                    "created_at": e["created_at"]})
    out.sort(key=lambda r: r["name"])
    return out


def metric_rows(company_id: int, limit: int = 500) -> list[dict]:
    rows = []
    for e in all_entries(company_id, "metrics", limit=limit):
        d = e["data"]
        rows.append({"id": e["id"], "company_id": company_id, "agent_id": e["agent_id"], "name": d.get("name", e["key"]),
                     "value": d.get("value", 0), "note": d.get("note", ""), "created_at": e["created_at"]})
    return rows


def money_summary(company_id: int) -> dict:
    rows = current(company_id, "transactions", limit=10000)
    month = db.now()[:7]
    by_cur: dict[str, dict] = {}
    for e in rows:
        d = e["data"]
        c = by_cur.setdefault(d.get("currency", "USD"), {"month_in": 0.0, "month_out": 0.0, "all_in": 0.0, "all_out": 0.0,
                                                        "by_category": {}, "count": 0})
        amt = float(d.get("amount", 0) or 0)
        inn = d.get("direction") == "in"
        c["count"] += 1
        c["all_in" if inn else "all_out"] += amt
        cat = c["by_category"].setdefault(d.get("category", "other"), {"in": 0.0, "out": 0.0, "month_in": 0.0, "month_out": 0.0})
        cat["in" if inn else "out"] += amt
        if str(d.get("date", "")).startswith(month):
            c["month_in" if inn else "month_out"] += amt
            cat["month_in" if inn else "month_out"] += amt
    main = max(by_cur, key=lambda k: by_cur[k]["count"]) if by_cur else "USD"
    m = by_cur.get(main, {"month_in": 0.0, "month_out": 0.0, "all_in": 0.0, "all_out": 0.0, "by_category": {}, "count": 0})
    for v in (m,):
        for k in ("month_in", "month_out", "all_in", "all_out"):
            v[k] = round(v[k], 2)
    return {"currency": main, "month": month, "transactions": m["count"], "month_in": m["month_in"], "month_out": m["month_out"],
            "month_net": round(m["month_in"] - m["month_out"], 2), "all_in": m["all_in"], "all_out": m["all_out"],
            "cash": round(m["all_in"] - m["all_out"], 2),
            "by_category": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(m["by_category"].items())},
            "other_currencies": sorted(k for k in by_cur if k != main)}


def contact_counts(company_id: int) -> dict:
    counts: dict[str, int] = {}
    for e in current(company_id, "contacts", limit=10000):
        st = e["data"].get("stage", "new")
        counts[st] = counts.get(st, 0) + 1
    return counts


def summary(company_id: int) -> dict:
    week_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "counts": {b: count(company_id, b) for b in BOOKS},
        "money": money_summary(company_id),
        "contacts": contact_counts(company_id),
        "handoffs_week": db.scalar("SELECT COUNT(*) FROM ledger WHERE company_id=? AND book='handoffs' AND current=1 AND "
                                   "created_at>=?", (company_id, week_ago)) or 0,
        "has_fts": HAS_FTS,
    }


def _money(v: float, cur: str) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "")
    sign = "-" if v < 0 else ""
    return f"{sign}{sym}{abs(v):,.2f}" + ("" if sym else f" {cur}")


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def entry_line(e: dict, book: str | None = None) -> str:
    """One line per entry, the way agents see it in tool results and prompts."""
    b = book or e["book"]
    d = e["data"]
    day = (e.get("created_at") or "")[:10]
    if b == "notes":
        return f"- [{d.get('kind', 'note')}] {_short(d.get('content') or e['summary'], 400)}"
    if b == "facts":
        return f"- {d.get('key', e['key'])} = {_short(str(d.get('value', '')), 300)}" + (f" (source: {_short(d['source'], 80)})" if d.get("source") else "") + f" [{day}]"
    if b == "decisions":
        line = f"- {day} {d.get('title', '')}: {_short(d.get('decision', ''), 300)}"
        if d.get("why"):
            line += f" Why: {_short(d['why'], 200)}"
        return line + f" ({'reversible' if d.get('reversible', True) else 'hard to reverse'}; by {d.get('by', '?')})"
    if b == "contacts":
        return f"- {_short(e['summary'], 300)}"
    if b == "transactions":
        return f"- {_short(e['summary'], 240)}"
    if b == "metrics":
        try:
            v = f"{float(d.get('value', 0)):g}"
        except (TypeError, ValueError):
            v = str(d.get("value"))
        return f"- {d.get('name', e['key'])} = {v}" + (f" ({_short(d['note'], 100)})" if d.get("note") else "") + f" [{day}]"
    if b == "handoffs":
        line = f"- Task #{d.get('task_id')} ({d.get('status')}) {d.get('title', '')} — {_short(d.get('summary', ''), 240)}"
        if d.get("open_issues"):
            line += f" Open: {_short(d['open_issues'], 120)}"
        return line + f" ({d.get('agent', '?')}, {day})"
    return f"- {_short(e['summary'], 300)}"


def text(company_id: int, book: str = "", query: str = "", key: str = "", limit: int = 25) -> str:
    """What the recall tool returns."""
    bk = book_name(book) if book else ""
    if key and bk:
        key = key.strip()
        if bk == "handoffs" and key.lstrip("#").isdigit():
            key = f"task:{key.lstrip('#')}"
        rows = history(company_id, bk, key.lower() if bk in ("contacts", "metrics", "facts") else key, limit)
        if not rows:
            # facts and metrics are keyed by normalised names
            rows = search(company_id, key, bk, limit)
        if not rows:
            return f"Nothing in {bk} for '{key}'."
        return f"{BOOKS[bk]['label']} history for {key} (newest first):\n" + "\n".join(entry_line(r, bk) for r in rows)
    if query.strip():
        rows = search(company_id, query, bk, limit)
        if not rows:
            return "Nothing found." + ("" if bk else " Try another word, or list a book with book=facts, decisions, contacts, money, metrics or handoffs.")
        return "\n".join(entry_line(r) for r in rows)
    if not bk:
        return overview_text(company_id, full=True)
    rows = current(company_id, bk, limit=limit)
    head = ""
    if bk == "transactions":
        m = money_summary(company_id)
        cur = m["currency"]
        cats = ", ".join(f"{CATEGORIES.get(k, (k, ''))[0]} {_money(v['in'] - v['out'], cur)}" for k, v in m["by_category"].items())
        head = (f"This month ({m['month']}): in {_money(m['month_in'], cur)}, out {_money(m['month_out'], cur)}, net "
                f"{_money(m['month_net'], cur)}. All time: in {_money(m['all_in'], cur)}, out {_money(m['all_out'], cur)}, "
                f"cash {_money(m['cash'], cur)}. By category (all time): {cats or 'none'}.\n"
                f"Full list in ledger/transactions.csv. Latest {len(rows)}:\n")
    elif bk == "contacts":
        counts = contact_counts(company_id)
        head = "Contacts by stage: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none") + \
               ". Full list in ledger/contacts.csv.\n"
    if not rows:
        return head + f"Nothing in {bk} yet."
    return head + "\n".join(entry_line(r, bk) for r in rows)


def overview_text(company_id: int, full: bool = False) -> str:
    """The ledger section of every agent's system prompt (compact), or a fuller overview for recall()."""
    parts = []
    pinned = [e for e in current(company_id, "notes", limit=40) if e["pinned"]][:12]
    if pinned:
        parts.append("Pinned:\n" + "\n".join(f"- {_short(e['data'].get('content') or e['summary'], 400)}" for e in pinned))
    facts = current(company_id, "facts", limit=24 if full else 18)
    if facts:
        parts.append("Facts:\n" + "\n".join(entry_line(e, "facts") for e in facts))
    decisions = current(company_id, "decisions", limit=8 if full else 5)
    if decisions:
        parts.append("Recent decisions:\n" + "\n".join(entry_line(e, "decisions") for e in decisions))
    notes = [e for e in current(company_id, "notes", limit=60) if not e["pinned"]][:14 if full else 10]
    if notes:
        parts.append("Recent notes:\n" + "\n".join(entry_line(e, "notes") for e in reversed(notes)))
    bits = []
    counts = contact_counts(company_id)
    if counts:
        bits.append("Contacts: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    m = money_summary(company_id)
    if m["transactions"]:
        cur = m["currency"]
        bits.append(f"Money this month: in {_money(m['month_in'], cur)}, out {_money(m['month_out'], cur)}; cash all time "
                    f"{_money(m['cash'], cur)}")
    hw = db.scalar("SELECT COUNT(*) FROM ledger WHERE company_id=? AND book='handoffs' AND current=1", (company_id,)) or 0
    if hw:
        bits.append(f"{hw} task handoffs on record (recall with book=handoffs)")
    if bits:
        parts.append("\n".join(f"- {b}" for b in bits))
    if full:
        mets = latest_metrics(company_id)
        if mets:
            parts.append("Latest metrics:\n" + "\n".join(f"- {r['name']} = {r['value']:g} ({r['created_at'][:10]})" for r in mets[:30]))
    return "\n\n".join(parts) or "(nothing recorded yet)"


# ---------------------------------------------------------------- files and export

README = """# Ledger

These files are generated from the company's ledger, the team's source of truth. Agents write to it with the
`remember`, `record_fact`, `record_decision`, `save_contact`, `record_transaction` and `record_metric` tools and
read it with `recall`; you can add and void entries on the Ledger tab of the dashboard.

Don't edit these files by hand: they are rewritten whenever the ledger changes. Every book is append-only
(entries are superseded or voided, never deleted), so the dashboard can always show how a value changed.

- facts.md: key = value facts (pricing, URLs, policies, market numbers), latest per key
- decisions.md: decisions with the reasoning and the options considered
- notes.md: pinned and recent free-form notes and lessons
- contacts.csv: leads, customers and partners with their pipeline stage
- transactions.csv: money in and out, categorised with the chart of accounts
- metrics.csv: every KPI reading, so trends can be computed
- handoffs.md: the end-of-task report of every finished task
"""


def _names(company_id: int) -> dict[int, str]:
    return {a["id"]: a["name"] for a in db.q("SELECT id, name FROM agents WHERE company_id=?", (company_id,))}


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


def export(company_id: int, book: str) -> tuple[str, str]:
    """(filename, content) for the Markdown or CSV rendering of a book."""
    bk = book_name(book)
    names = _names(company_id)
    who = lambda e: names.get(e["agent_id"], "owner" if e["agent_id"] is None else "agent")  # noqa: E731
    if bk == "contacts":
        rows = [{**e["data"], "updated_at": e["created_at"], "by": who(e)} for e in current(company_id, bk, limit=100000)]
        rows.sort(key=lambda r: (r.get("stage", ""), r.get("company", ""), r.get("name", "")))
        return "contacts.csv", _csv(rows, ["email", "name", "company", "role", "stage", "do_not_contact", "source", "notes",
                                           "url", "first_seen", "touches", "updated_at", "by"])
    if bk == "transactions":
        rows = [{**e["data"], "recorded_at": e["created_at"], "by": who(e)} for e in current(company_id, bk, limit=100000)]
        rows.sort(key=lambda r: (r.get("date", ""), r.get("recorded_at", "")))
        return "transactions.csv", _csv(rows, ["date", "amount", "currency", "direction", "category", "counterparty",
                                               "description", "external_id", "provider", "recorded_at", "by"])
    if bk == "metrics":
        rows = [{**e["data"], "recorded_at": e["created_at"], "by": who(e)} for e in all_entries(company_id, bk, limit=100000)]
        rows.sort(key=lambda r: (r.get("name", ""), r.get("recorded_at", "")))
        return "metrics.csv", _csv(rows, ["name", "value", "note", "period", "recorded_at", "by"])
    if bk == "facts":
        rows = current(company_id, bk, limit=5000)
        lines = ["# Facts", "", "Generated from the ledger; the latest value per key. Don't edit by hand.", "",
                 "| Key | Value | Source | Updated | By |", "|---|---|---|---|---|"]
        for e in sorted(rows, key=lambda r: r["key"]):
            d = e["data"]
            cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
            lines.append(f"| {cell(d.get('key', e['key']))} | {cell(d.get('value', ''))} | {cell(d.get('source', ''))} | "
                         f"{e['created_at'][:10]} | {who(e)} |")
        return "facts.md", "\n".join(lines) + "\n"
    if bk == "decisions":
        lines = ["# Decisions (newest first)", "", "Generated from the ledger. Don't edit by hand.", ""]
        for e in current(company_id, bk, limit=5000):
            d = e["data"]
            lines += [f"## {e['created_at'][:10]} {d.get('title', '')}", "", f"**Decision:** {d.get('decision', '')}", ""]
            if d.get("why"):
                lines += [f"**Why:** {d['why']}", ""]
            if d.get("options"):
                lines += [f"**Options considered:** {d['options']}", ""]
            lines += [f"**Reversible:** {'yes' if d.get('reversible', True) else 'no'}. **By:** {d.get('by', who(e))}.", ""]
        return "decisions.md", "\n".join(lines) + "\n"
    if bk == "handoffs":
        lines = ["# Task handoffs (newest first)", "", "Generated from the ledger. Don't edit by hand.", ""]
        for e in current(company_id, bk, limit=300):
            d = e["data"]
            lines += [f"## Task #{d.get('task_id')}: {d.get('title', '')} ({d.get('status')})",
                      f"{d.get('agent', '')}, {e['created_at'][:16].replace('T', ' ')}", ""]
            for label, k in (("Summary", "summary"), ("Deliverable", "deliverable"), ("Evidence", "evidence"),
                             ("Open issues", "open_issues"), ("Next step", "next_step")):
                if d.get(k):
                    lines.append(f"- **{label}:** {d[k]}")
            lines.append("")
        return "handoffs.md", "\n".join(lines) + "\n"
    rows = current(company_id, "notes", limit=600)
    lines = ["# Notes", "", "Generated from the ledger. Pinned notes first, then the most recent. Don't edit by hand.", ""]
    for e in rows:
        d = e["data"]
        lines.append(f"- {'📌 ' if e['pinned'] else ''}[{d.get('kind', 'note')}] {d.get('content', e['summary'])} "
                     f"({who(e)}, {e['created_at'][:10]})")
    return "notes.md", "\n".join(lines) + "\n"


def render(company: dict, book: str) -> str:
    """Write a book's file into the company folder. Returns the relative path."""
    from . import workspace
    bk = book_name(book)
    name, content = export(company["id"], bk)
    with _render_lock:
        folder = workspace.safe_path(company, "ledger")
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "README.md"
        if not readme.exists():
            readme.write_text(README, encoding="utf-8", newline="")
        (folder / name).write_text(content, encoding="utf-8", newline="")
    return f"ledger/{name}"


def render_all(company: dict) -> None:
    for b in BOOKS:
        try:
            render(company, b)
        except Exception:
            pass
