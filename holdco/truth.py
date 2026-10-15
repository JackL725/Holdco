"""Ground truth (Phase 0.5): the facts block, the records folder, changes since an agent's last session, the
claims gate, action-task rules, write guards, outreach checks, the fetched-page cache and verified facts.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, sections 2 and 5. Every function here is a seam that
tools.py, agents.py and actions.py already call.

Why this module exists: in the first real run the agents never saw what had really happened, so they kept state
in files and then believed them (a "deployed" confirmation for a deploy still waiting for the owner, an email
"sent" log with no mailbox connected). Everything an agent is told about the outside world here comes from the
database. Guards fail open on an internal error (a bug here must never stop all work), and nothing in this module
prints a traceback, because the end-to-end tests treat one in the server log as a failure.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import html as _html
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from . import bus, checks, db, emailer, workspace
from .actions import ACTION_LABELS
from .routing import company_or_404, route
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401  (tools register here)

FACTS_HEADER = "# Facts (from Holdco's records; these override anything in files)"
FACTS_CAP = 2000
CHANGES_CAP = 1200
WAITING_CAP = 800
WAITING_LINES = 8
STATUS_CAP = 7000
DEBOUNCE_SECONDS = 2.0
PAGE_TTL_DAYS = 7
MIN_QUOTE = 12

# Fetched pages live in .cache/pages for record_fact. They are Holdco's working data, not the company's work, so
# file listings (agents and dashboard) skip the folder like .runs and .trash.
# workspace.SKIP lists .cache (fetched pages) so file listings and the index skip it.

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _warn(what: str, e: Exception) -> None:
    """One line, never a traceback: the records and guards are a view, and a failure must stay quiet."""
    print(f"truth: {what} failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- time

def _now_local() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _today() -> _dt.date:
    return _now_local().date()


def _long_date(d: _dt.date) -> str:
    # Spelled out by hand: strftime's names follow the PC's locale, and agents read English.
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _zone(n: _dt.datetime) -> str:
    off = n.utcoffset() or _dt.timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    utc = f"UTC{sign}{mins // 60:02d}:{mins % 60:02d}"
    name = (n.tzname() or "").strip()
    if not name or name.upper().startswith("UTC") or name[0] in "+-":
        return utc
    return f"{name}, {utc}"


def now_text() -> str:
    """'Thursday 15 October 2026, 09:40 (BST, UTC+01:00)': the PC's local time with its zone."""
    n = _now_local()
    return f"{_long_date(n.date())}, {n:%H:%M} ({_zone(n)})"


def _local(ts: str | None) -> _dt.datetime | None:
    t = db.parse_time(ts)
    return t.astimezone() if t else None


def _clock(ts: str | None) -> str:
    t = _local(ts)
    if not t:
        return "an unknown time"
    if t.date() == _today():
        return t.strftime("%H:%M")
    return f"{t.day} {_MONTHS[t.month - 1][:3]} {t:%H:%M}"


def _age_short(ts: str | None) -> str:
    m = db.minutes_since(ts)
    if m >= 1e8:
        return "?"
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{int(m)} min"
    if m < 48 * 60:
        return f"{int(m // 60)} h"
    return f"{int(m // 1440)} days"


def _age_words(ts: str | None) -> str:
    m = db.minutes_since(ts)
    if m >= 1e8:
        return ""
    if m < 1:
        return "less than a minute"
    if m < 60:
        n = int(m)
        return f"{n} minute{'s' if n != 1 else ''}"
    if m < 48 * 60:
        n = int(m // 60)
        return f"{n} hour{'s' if n != 1 else ''}"
    return f"{int(m // 1440)} days"


# ---------------------------------------------------------------- small helpers

def _addr(s: str | None) -> str:
    return (email.utils.parseaddr(s or "")[1] or "").strip().lower()


def _host(url: str | None) -> str:
    m = re.match(r"^\s*(?:[a-z][a-z0-9+.-]*://)?([^/\s:?#]+)", url or "", re.I)
    host = m.group(1).lower().rstrip(".") if m else ""
    return host[4:] if host.startswith("www.") else host


def _deploy_ok(result) -> bool:
    """Only a real Netlify upload counts. 'Approved, but no Netlify token is set' put nothing online."""
    return str(result or "").startswith("Website deployed")


def _post_sent(result) -> bool:
    """A post went out only through the webhook; otherwise the owner got a link to post by hand."""
    return str(result or "").startswith("Sent to the posting webhook")


def _one_line(s, n: int = 120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _payload(row: dict) -> dict:
    return db.loads(row.get("payload"), {}) or {}


def _cap(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 4].rstrip() + "\n..."


# ---------------------------------------------------------------- the state, gathered once per use

def _real_metrics(cid: int) -> list[dict]:
    """Metrics recorded by a connector. Phase 1 adds metrics.source; until then nothing counts as real, because
    record_metric lets an agent type any number."""
    try:
        cols = {r["name"] for r in db.q("PRAGMA table_info(metrics)")}
        if "source" not in cols:
            return []
        return db.q("SELECT name, value, source, created_at FROM metrics WHERE company_id=? AND "
                    "COALESCE(source, '') NOT IN ('', 'manual', 'agent') ORDER BY id DESC LIMIT 100", (cid,))
    except Exception:
        return []


def _down(cid: int) -> list[dict]:
    """What the Doctor last found broken that changes what agents can do (web search and this company's
    connectors), plus a search outage the agents hit since. Backups, disk and the owner's phone are the owner's
    business, not the agents'."""
    out: list[dict] = []
    try:
        from . import doctor   # imported here: doctor imports notify, which must not load during tools' import
        for r in list(doctor.LAST):
            if not isinstance(r, dict) or r.get("status") != "bad":
                continue
            key = str(r.get("key") or "")
            if not (key == "search" or (key.startswith(("connector:", "conn:")) and r.get("company_id") == cid)):
                continue
            out.append({"key": r.get("key", ""), "label": r.get("label") or r.get("key", ""),
                        "detail": _one_line(r.get("detail"), 160), "checked_at": r.get("checked_at")})
    except Exception:
        pass
    try:
        from . import web
        if web.SEARCH_FAIL_STREAK >= 2 and not any(d["key"] == "search" for d in out):
            out.append({"key": "search", "label": "Web search",
                        "detail": f"every provider failed on the last {web.SEARCH_FAIL_STREAK} searches",
                        "checked_at": db.now()})
    except Exception:
        pass
    return out


def _state(company: dict) -> dict:
    """Everything the facts block, the records and the claims gate need, read from the database."""
    cid = company["id"]
    company = db.get_company(cid) or company   # fresh: a deploy writes site_url into the settings
    cs = db.company_settings(company)
    names = {a["id"]: a["name"] for a in db.q("SELECT id, name FROM agents WHERE company_id=?", (cid,))}
    approvals = db.q("SELECT * FROM approvals WHERE company_id=? ORDER BY id DESC LIMIT 2000", (cid,))
    pending = sorted((a for a in approvals if a["status"] == "pending"), key=lambda a: a["id"])
    audits = db.q("SELECT * FROM audit WHERE company_id=? ORDER BY id DESC LIMIT 2000", (cid,))
    st: dict = {"company": company, "cs": cs, "names": names, "approvals": approvals, "pending": pending,
                "audits": audits}

    # ---- website: a deploy counts only when the audit log shows a real upload
    try:
        has_index = workspace.safe_path(company, "site/index.html").exists()
    except Exception:
        has_index = False
    good = [r for r in audits if r["action"] == "deploy_site" and _deploy_ok(r["result"])]
    site_url = str(cs.get("site_url") or "").strip()
    urls = []
    for r in good:
        m = re.search(r"https?://\S+", r["result"] or "")
        if m:
            urls.append(m.group(0).rstrip(".,;)"))
    live_url = site_url if (good and site_url) else (urls[0] if urls else "")
    st["deploy_hosts"] = {_host(u) for u in urls + ([site_url] if good and site_url else []) if u}
    dep_pending = [a for a in pending if a["action"] == "deploy_site"]
    dep_decided = [a for a in approvals if a["action"] == "deploy_site" and a["status"] in ("done", "failed")]
    if good:
        line = f"Website: live at {live_url or 'its Netlify address'} (last deploy {_clock(good[0]['created_at'])})."
        if dep_pending:
            p = dep_pending[0]
            line += (f" Deploy request #{p['id']} for newer changes is waiting for the owner since "
                     f"{_clock(p['created_at'])}.")
    else:
        line = "Website: local preview only; not deployed."
        if not has_index:
            line += " There is no site/index.html yet."
        if dep_pending:
            p = dep_pending[0]
            line += (f" Deploy request #{p['id']} waiting for the owner since {_clock(p['created_at'])} "
                     f"({_age_short(p['created_at'])}).")
        elif dep_decided:
            d = dep_decided[0]
            if d["status"] == "done":
                line += (f" Deploy request #{d['id']} was approved, but no Netlify token is set, so nothing went "
                         "online.")
            else:
                line += f" Deploy request #{d['id']} failed: {_one_line(d['result'], 100)}."
        if site_url:
            line += f" {site_url} is in Settings, but Holdco has no record of a deploy to it."
    st["website"] = {"deployed": bool(good), "url": live_url, "site_url": site_url, "has_index": has_index,
                     "last_deploy_at": good[0]["created_at"] if good else None,
                     "pending": [{"id": a["id"], "since": a["created_at"], "age": _age_short(a["created_at"])}
                                 for a in dep_pending],
                     "line": line}

    # ---- email: the emails table only has outgoing rows for mail that really left
    out_rows = db.q("SELECT to_addr, created_at FROM emails WHERE company_id=? AND direction='out'", (cid,))
    in_rows = db.q("SELECT from_addr, created_at FROM emails WHERE company_id=? AND direction='in'", (cid,))
    connected = emailer.smtp_configured(cs)
    inbox = emailer.imap_configured(cs)
    today = db.now()[:10]   # the daily limit in tools.send_email counts by this (UTC) date too
    sent_today = sum(1 for r in out_rows if (r["created_at"] or "") >= today)
    em_pending = [a for a in pending if a["action"] == "send_email"]
    try:
        limit = int(cs.get("daily_email_limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    sender = str(cs.get("smtp_from") or cs.get("smtp_user") or "")
    if connected:
        line = (f"Email: connected, sending as {sender}. Sent today: {sent_today} of {limit}. Sent in total: "
                f"{len(out_rows)}. Replies received: {len(in_rows)}.")
        if em_pending:
            line += f" {_plural(len(em_pending), 'email')} waiting for the owner's approval."
        if not inbox:
            line += " The inbox is not connected, so replies cannot be read."
    else:
        line = (f"Email: not connected: you cannot send email until the owner adds a mailbox. Sent so far: "
                f"{len(out_rows)}.")
        if em_pending:
            line += (f" {_plural(len(em_pending), 'email request')} queued; none can go out until a mailbox is "
                     "connected.")
    st["email"] = {"connected": connected, "inbox_connected": inbox, "from": sender if connected else "",
                   "sent_today": sent_today, "daily_limit": limit, "sent_total": len(out_rows),
                   "replies": len(in_rows), "pending": len(em_pending), "line": line}
    st["out_addrs"] = {_addr(r["to_addr"]) for r in out_rows}
    st["in_addrs"] = {_addr(r["from_addr"]) for r in in_rows}

    # ---- social posting
    sent_posts = [r for r in audits if r["action"] == "social_post" and _post_sent(r["result"])]
    manual_posts = [a for a in approvals if a["action"] == "social_post" and a["status"] == "done"
                    and not _post_sent(a["result"])]
    post_pending = [a for a in pending if a["action"] == "social_post"]
    webhook = str(cs.get("webhook_url") or "").strip()
    if webhook:
        line = (f"Social posting: through the owner's posting webhook ({_host(webhook)}); approved posts go out "
                f"through it. Posts sent through Holdco: {len(sent_posts)}.")
    else:
        line = ("Social posting: no webhook or account. Approved posts become a link for the owner to post by hand. "
                f"Posts sent through Holdco: {len(sent_posts)}.")
    st["social"] = {"webhook": bool(webhook), "sent": len(sent_posts), "handed_to_owner": len(manual_posts),
                    "pending": len(post_pending), "line": line}
    st["manual_posts"] = manual_posts

    # ---- accounts: only what Holdco can see configured (the real run linked to a Substack it never had)
    accounts = []
    if good or site_url:
        host = _host(live_url or site_url)
        accounts.append({"kind": "website", "label": f"website {host}" + ("" if good else " (not deployed)")})
        if host and not host.endswith((".netlify.app", ".vercel.app", ".pages.dev", ".github.io")):
            accounts.append({"kind": "domain", "label": f"domain {host}"})
    if connected:
        accounts.append({"kind": "mailbox", "label": f"mailbox {sender}"})
    if inbox:
        accounts.append({"kind": "inbox", "label": "inbox (reading replies)"})
    if cs.get("netlify_token"):
        accounts.append({"kind": "netlify", "label": "Netlify hosting"})
    if webhook:
        accounts.append({"kind": "webhook", "label": f"posting webhook ({_host(webhook)})"})
    have = ", ".join(a["label"] for a in accounts) or "none besides the local website"
    st["accounts"] = accounts
    st["accounts_line"] = (f"Accounts this company has: {have}. Do not link to or claim any other account "
                           "(domain, Substack, analytics, payment page, social profile) until it appears here.")

    # ---- money
    bought = [a for a in approvals if a["action"] == "spend_money" and a["status"] == "done"]
    total = 0.0
    for a in bought:
        try:
            total += float(_payload(a).get("amount_usd") or 0)
        except (TypeError, ValueError):
            pass
    buy_pending = [a for a in pending if a["action"] == "spend_money"]
    if bought:
        items = ", ".join(_one_line(_payload(a).get("item"), 40) for a in bought[:3])
        line = (f"Money: ${total:,.2f} approved by the owner for {_plural(len(bought), 'purchase')} ({items}). "
                "Purchases always need the owner.")
    else:
        line = "Money: $0 spent. Purchases always need the owner."
    st["money"] = {"approved_usd": round(total, 2), "purchases": len(bought), "pending": len(buy_pending),
                   "line": line}

    # ---- waiting on the owner
    waiting = []
    for a in pending:
        is_q = a["action"] == "question"
        waiting.append({"id": a["id"], "kind": "question" if is_q else "approval", "action": a["action"],
                        "label": "Question" if is_q else ACTION_LABELS.get(a["action"], a["action"]),
                        "summary": _one_line(a["summary"], 160), "created_at": a["created_at"],
                        "age": _age_short(a["created_at"]), "asked_by": names.get(a["agent_id"], "")})
    st["waiting"] = waiting
    qs = [w for w in waiting if w["kind"] == "question"]
    aps = [w for w in waiting if w["kind"] == "approval"]
    if not waiting:
        st["waiting_line"] = "Waiting on the owner: nothing."
    else:
        bits = []
        if qs:
            shown = ", ".join(f"#{w['id']}, {w['age']}" for w in qs[:4]) + \
                (f", +{len(qs) - 4} more" if len(qs) > 4 else "")
            bits.append(f"{_plural(len(qs), 'question')} ({shown})")
        if aps:
            shown = ", ".join(f"#{w['id']}, {w['label'].lower()}" for w in aps[:4]) + \
                (f", +{len(aps) - 4} more" if len(aps) > 4 else "")
            bits.append(f"{_plural(len(aps), 'approval')} ({shown})")
        st["waiting_line"] = "Waiting on the owner: " + ", ".join(bits) + "."

    # ---- signups, revenue, replies: only from a real source
    real = _real_metrics(cid)
    signup_rows = [m for m in real if re.search(r"sign|subscri|waitlist|member|registr|join", m["name"] or "", re.I)]
    revenue_rows = [m for m in real if re.search(r"revenue|mrr|sale|paid|payment|order", m["name"] or "", re.I)]
    form_rows = [m for m in real if re.search(r"form|submission", m["name"] or "", re.I)]
    parts = []
    if signup_rows:
        parts.append(f"signups {signup_rows[0]['value']:g} ({signup_rows[0]['source']})")
    if revenue_rows:
        parts.append(f"revenue {revenue_rows[0]['value']:g} ({revenue_rows[0]['source']})")
    if in_rows:
        parts.append(f"replies: {_plural(len(in_rows), 'inbound email')} from "
                     f"{_plural(len(st['in_addrs']), 'address', 'addresses')}")
    if not parts:
        line = "Signups, revenue, replies: none recorded from a real source."
    else:
        missing = [w for w, rows in (("signups", signup_rows), ("revenue", revenue_rows), ("replies", in_rows))
                   if not rows]
        line = "Recorded from a real source: " + "; ".join(parts) + "."
        if missing:
            line += f" {' and '.join(missing).capitalize()}: none."
    st["signals"] = {"signups": [dict(m) for m in signup_rows[:5]], "revenue": [dict(m) for m in revenue_rows[:5]],
                     "replies": len(in_rows), "line": line}
    st["signup_ok"] = bool(signup_rows)
    st["form_ok"] = bool(form_rows)

    # ---- down right now
    down = _down(cid)
    st["down"] = down
    if down:
        st["down_line"] = "Down right now: " + "; ".join(
            f"{d['label'].lower()} ({_one_line(d['detail'], 70)}, checked {_clock(d['checked_at'])})"
            for d in down[:4]) + "."
    else:
        st["down_line"] = "Down right now: nothing Holdco knows of."

    age = _age_words(company.get("created_at"))
    st["now_line"] = f"Now: {now_text()}." + (f" Company founded {age} ago." if age else "")
    return st


def _facts_text(st: dict, cap: int | None = FACTS_CAP) -> str:
    lines = [FACTS_HEADER, st["now_line"], st["website"]["line"], st["email"]["line"], st["social"]["line"],
             st["accounts_line"], st["money"]["line"], st["waiting_line"], st["signals"]["line"], st["down_line"]]
    text = "\n".join(lines)
    if cap and len(text) > cap:
        # Long lines (many accounts, a long outage detail) are shortened before anything is dropped.
        lines = [_one_line(x, 260) if i else x for i, x in enumerate(lines)]
        text = _cap("\n".join(lines), cap)
    return text


# ---------------------------------------------------------------- prompt sections

def facts_section(company: dict) -> str:
    """'# Facts' block generated from the database (design 2.1), capped at 2,000 characters."""
    try:
        return _facts_text(_state(company))
    except Exception as e:
        _warn("facts", e)
        return f"{FACTS_HEADER}\nNow: {now_text()}.\n(The rest of the facts could not be read this time.)"


def _previous_run(agent: dict, kind: str) -> dict | None:
    rows = db.q("SELECT id, kind, status, started_at FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT 60",
                (agent["id"],))
    if rows and rows[0]["status"] == "running":
        rows = rows[1:]   # the run being set up right now (agents.run inserts it before building the prompt)
    if kind == "cycle":
        cycles = [r for r in rows if r["kind"] == "cycle"]
        if cycles:
            return cycles[0]
    return rows[0] if rows else None


def _change_items(company: dict, agent: dict, since: str, kind: str = "task") -> list[tuple[str, str]]:
    """(timestamp, line) for what changed around this agent since `since`. The agent's own actions are left out:
    its earlier work shows in task handoffs and notes, and counting it would mean a planning cycle never sees
    'nothing changed'."""
    cid, me, my_name = company["id"], agent["id"], agent.get("name") or ""
    names = {a["id"]: a["name"] for a in db.q("SELECT id, name FROM agents WHERE company_id=?", (cid,))}
    items: list[tuple[str, str]] = []
    for t in db.q("SELECT * FROM tasks WHERE company_id=? AND created_at>=? ORDER BY id DESC LIMIT 40",
                  (cid, since)):
        if (t["created_by"] or "") == my_name:
            continue
        who = "the owner" if t["created_by"] == "owner" else (t["created_by"] or "someone")
        items.append((t["created_at"],
                      f"Task #{t['id']} created by {who} for {t['role']}: {_one_line(t['title'], 70)}"))
    for t in db.q("SELECT * FROM tasks WHERE company_id=? AND updated_at>=? AND status IN "
                  "('done','blocked','cancelled','waiting') ORDER BY updated_at DESC LIMIT 40", (cid, since)):
        if t["agent_id"] == me and t["status"] != "cancelled":
            continue
        who = names.get(t["agent_id"], t["role"] or "someone")
        what = {"done": f"{who} finished task #{t['id']}", "blocked": f"Task #{t['id']} ({who}) is blocked",
                "cancelled": f"Task #{t['id']} was cancelled",
                "waiting": f"Task #{t['id']} ({who}) waits on the owner"}
        items.append((t["updated_at"], f"{what[t['status']]}: {_one_line(t['title'], 70)}"))
    seen_paths: set = set()
    for e in db.q("SELECT * FROM events WHERE company_id=? AND kind='file' AND created_at>=? ORDER BY id DESC "
                  "LIMIT 80", (cid, since)):
        if e["agent_id"] == me:
            continue
        path = (db.loads(e["data"], {}) or {}).get("path") or e["message"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        who = names.get(e["agent_id"], "Someone") if e["agent_id"] else "The owner"
        items.append((e["created_at"], f"{who} {_one_line(e['message'], 90)}"))
    for a in db.q("SELECT * FROM approvals WHERE company_id=? AND decided_at>=? AND status!='pending' "
                  "ORDER BY decided_at DESC LIMIT 30", (cid, since)):
        label = ACTION_LABELS.get(a["action"], a["action"])
        if a["action"] == "question":
            line = (f"The owner answered question #{a['id']} ({_one_line(a['summary'], 60)}): "
                    f"{_one_line(a['result'], 110)}")
        elif a["status"] == "rejected":
            line = f"The owner rejected request #{a['id']} ({label})" + \
                (f": {_one_line(a['feedback'], 90)}" if a["feedback"] else ".")
        elif a["status"] == "failed":
            line = f"Request #{a['id']} ({label}) was approved but failed: {_one_line(a['result'], 90)}"
        else:
            line = f"The owner approved request #{a['id']} ({label}). Outcome: {_one_line(a['result'], 90)}"
        items.append((a["decided_at"], line))
    for e in db.q("SELECT * FROM emails WHERE company_id=? AND direction='in' AND created_at>=? ORDER BY id DESC "
                  "LIMIT 20", (cid, since)):
        items.append((e["created_at"], f"New email #{e['id']} from {_addr(e['from_addr']) or e['from_addr']}: "
                                       f"{_one_line(e['subject'], 70)}"))
    for a in db.q("SELECT * FROM audit WHERE company_id=? AND approval_id IS NULL AND created_at>=? "
                  "ORDER BY id DESC LIMIT 20", (cid, since)):
        if (a["proposed_by"] or "") == my_name:
            continue
        items.append((a["created_at"], f"{ACTION_LABELS.get(a['action'], a['action'])} ran automatically: "
                                       f"{_one_line(a['result'], 90)}"))
    for f in db.q("SELECT * FROM facts WHERE company_id=? AND created_at>=? ORDER BY id DESC LIMIT 20",
                  (cid, since)):
        if f["agent_id"] == me:
            continue
        items.append((f["created_at"], f"{names.get(f['agent_id'], 'Someone')} recorded fact #{f['id']}: "
                                       f"{_one_line(f['claim'], 90)}"))
    chat = db.q("SELECT * FROM messages WHERE company_id=? AND sender='owner' AND created_at>=? ORDER BY id DESC "
                "LIMIT 10", (cid, since))
    for m in (chat[1:] if kind == "chat" else chat):   # a chat run already has the newest message as its prompt
        items.append((m["created_at"], f"The owner wrote in chat: {_one_line(m['content'], 110)}"))
    for e in db.q("SELECT * FROM events WHERE company_id=? AND kind='error' AND message LIKE "
                  "'Web search is unavailable%' AND created_at>=? ORDER BY id DESC LIMIT 1", (cid, since)):
        items.append((e["created_at"], "Web search went down"))
    for d in _down(cid):
        if (d.get("checked_at") or "") >= since and d["key"] != "search":
            items.append((d["checked_at"], f"{d['label']} is down: {_one_line(d['detail'], 80)}"))
    items.sort(key=lambda x: x[0] or "", reverse=True)
    return items


def changes_section(company: dict, agent: dict, kind: str) -> str:
    """'# Since your last session' for this agent (design 2.3): newest first, capped at 1,200 characters.
    Empty for an agent's first run."""
    try:
        prev = _previous_run(agent, kind)
        if not prev or not prev.get("started_at"):
            return ""
        items = _change_items(company, agent, prev["started_at"], kind)
        if not items:
            if kind == "cycle":
                return "# Since your last session\nNothing has changed since your last planning cycle."
            return ""
        head = f"# Since your last session (started {_clock(prev['started_at'])}, newest first)"
        out, used = [head], len(head)
        for i, (ts, line) in enumerate(items):
            row = f"- {_clock(ts)} {line}"
            rest = len(items) - i
            tail = f"- ...and {rest} older change{'s' if rest != 1 else ''} (use search or list_tasks)"
            if used + 1 + len(row) + (1 + len(tail) if rest > 1 else 0) > CHANGES_CAP:
                out.append(tail)
                break
            out.append(row)
            used += len(row) + 1
        return _cap("\n".join(out), CHANGES_CAP)
    except Exception as e:
        _warn("changes", e)
        return ""


def _waiting_text(waiting: list[dict]) -> str:
    if not waiting:
        return ""
    advice = ("Do not ask about these again. Work on something that does not need them, or finish with status "
              "'waiting'.")
    room = WAITING_LINES - 2   # the heading and the advice take two of the eight lines
    shown = waiting if len(waiting) <= room else waiting[: room - 1]
    lines = ["# Waiting on the owner"]
    for w in shown:
        who = "Question" if w["kind"] == "question" else f"Request ({w['label']})"
        lines.append(f"- {who} #{w['id']}, {w['age']}: {_one_line(w['summary'], 90)}")
    if len(shown) < len(waiting):
        lines.append(f"- ...and {len(waiting) - len(shown)} more (records/owner.md and records/outbound.md)")
    lines.append(advice)
    text = "\n".join(lines)
    while len(text) > WAITING_CAP and len(lines) > 3:
        lines.pop(-2)
        text = "\n".join(lines)
    return _cap(text, WAITING_CAP)


def waiting_section(company: dict) -> str:
    """'# Waiting on the owner': pending questions and approvals with ids and age, at most 8 lines."""
    try:
        return _waiting_text(_state(company)["waiting"])
    except Exception as e:
        _warn("waiting", e)
        return ""


# ---------------------------------------------------------------- records

_records_lock = threading.RLock()
_timer_lock = threading.Lock()
_timers: dict[int, threading.Timer] = {}
_last_write: dict[int, float] = {}
_scanned: set[int] = set()


def _write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and a rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
            return
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    try:
        os.replace(tmp, path)
    except OSError:
        time.sleep(0.05)   # Windows: another program has the file open for a moment
        try:
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _outbound_status(a: dict) -> str:
    s, act, res = a["status"], a["action"], a["result"] or ""
    if s == "pending":
        return "queued, waiting for the owner"
    if s == "running":
        return "approved, running now"
    if s == "rejected":
        return "rejected by the owner" + (f" (feedback: {_one_line(a['feedback'], 120)})" if a.get("feedback") else "")
    if s == "failed":
        return f"approved, but it failed: {_one_line(res, 160)}"
    if s == "done":
        if act == "send_email":
            return "approved and sent"
        if act == "deploy_site":
            return "approved and deployed" if _deploy_ok(res) else "approved, but not deployed (no Netlify token)"
        if act == "social_post":
            return "approved and posted through the webhook" if _post_sent(res) else \
                "approved and handed to the owner to post by hand (no record it went out)"
        if act == "spend_money":
            return "approved; the owner makes the purchase"
        return "approved and ran"
    return s


def _status_text(st: dict) -> str:
    c = st["company"]
    lines = [f"# {c['name']}: status", "Written by Holdco from its database at the start of every run and after every "
             "outbound action or owner decision. Agents cannot edit this folder.", "", _facts_text(st, cap=None), "",
             "## Waiting on the owner"]
    if st["waiting"]:
        for w in st["waiting"]:
            who = "Question" if w["kind"] == "question" else f"Request ({w['label']})"
            lines.append(f"- {who} #{w['id']}, asked {w['age']} ago by {w['asked_by'] or 'an agent'}: "
                         f"{_one_line(w['summary'], 200)}")
    else:
        lines.append("Nothing is waiting on the owner.")
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tasks WHERE company_id=? "
                                                "GROUP BY status", (c["id"],))}
    open_n = sum(counts.get(k, 0) for k in ("todo", "in_progress", "waiting", "blocked"))
    lines += ["", "## Tasks",
              f"Open: {open_n} ({counts.get('todo', 0)} to do, {counts.get('in_progress', 0)} in progress, "
              f"{counts.get('waiting', 0)} waiting on the owner, {counts.get('blocked', 0)} blocked). "
              f"Done: {counts.get('done', 0)}. Cancelled: {counts.get('cancelled', 0)}.", "",
              "## Latest outbound requests"]
    recent = [a for a in st["approvals"] if a["action"] != "question"][:8]
    if recent:
        for a in recent:
            lines.append(f"- #{a['id']} {ACTION_LABELS.get(a['action'], a['action'])}: "
                         f"{_one_line(a['summary'], 100).rstrip('.')}. Status: {_outbound_status(a)}.")
    else:
        lines.append("None yet.")
    lines += ["", "Full history: records/outbound.md, records/emails.md and records/owner.md."]
    return "\n".join(lines) + "\n"


def _outbound_text(st: dict) -> str:
    from .actions import _summary
    names = st["names"]
    rows = []
    for a in st["approvals"]:
        if a["action"] == "question":
            continue
        who = names.get(a["agent_id"], "an agent")
        line = (f"- #{a['id']} {ACTION_LABELS.get(a['action'], a['action'])}: {_one_line(a['summary'], 160).rstrip('.')}. "
                f"Asked by {who}, {_clock(a['created_at'])}. Status: {_outbound_status(a)}.")
        if a["decided_at"]:
            line += f" Decided {_clock(a['decided_at'])}."
        if a["result"] and a["status"] in ("done", "failed"):
            line += f" Result: {_one_line(a['result'], 200)}"
        rows.append((a["created_at"] or "", line))
    for r in st["audits"]:
        if r["approval_id"] is not None:
            continue   # already listed with its approval
        try:
            summary = _summary(r["action"], db.loads(r["payload"], {}) or {})
        except Exception:
            summary = r["action"]
        rows.append((r["created_at"] or "", f"- {ACTION_LABELS.get(r['action'], r['action'])}: "
                                            f"{_one_line(summary, 160).rstrip('.')}. Proposed by {r['proposed_by'] or 'an agent'}, "
                                            f"ran automatically ({r['approved_by']}) {_clock(r['created_at'])}. "
                                            f"Result: {_one_line(r['result'], 200)}"))
    rows.sort(key=lambda x: x[0], reverse=True)
    head = ["# Outbound requests", "Every outbound request and what happened to it, newest first. Written by Holdco "
            "from its records; agents cannot edit it.", ""]
    body = [r[1] for r in rows[:300]] or ["No outbound request has been made yet."]
    return "\n".join(head + body) + "\n"


def _emails_text(st: dict) -> str:
    rows = db.q("SELECT * FROM emails WHERE company_id=? ORDER BY id DESC LIMIT 100", (st["company"]["id"],))
    head = ["# Emails", "The last 100 emails sent and received, newest first. Written by Holdco from the mailbox "
            "records; agents cannot edit it.",
            "Email is connected." if st["email"]["connected"] else
            "Email is not connected: nothing can be sent until the owner adds a mailbox.", ""]
    body = []
    for e in rows:
        if e["direction"] == "out":
            body.append(f"- {_clock(e['created_at'])} sent to {e['to_addr']}: \"{_one_line(e['subject'], 100)}\" "
                        f"({e['status']})")
        else:
            body.append(f"- {_clock(e['created_at'])} received from {e['from_addr']}: "
                        f"\"{_one_line(e['subject'], 100)}\" ({e['status']})")
        body.append(f"  {_one_line(e['body'], 300)}")
    return "\n".join(head + (body or ["No email has been sent or received yet."])) + "\n"


def _owner_text(st: dict) -> str:
    names = st["names"]
    lines = ["# Questions to the owner and answers", "Newest first. Written by Holdco from its records; agents cannot "
             "edit it.", ""]
    qs = [a for a in st["approvals"] if a["action"] == "question"][:200]
    for a in qs:
        p = _payload(a)
        lines.append(f"- Question #{a['id']} from {names.get(a['agent_id'], 'Holdco')}, {_clock(a['created_at'])}: "
                     f"{_one_line(p.get('question') or a['summary'], 400)}")
        if p.get("options"):
            lines.append(f"  Options: {' / '.join(_one_line(o, 60) for o in p['options'])}"
                         + (f". Recommended: {_one_line(p.get('recommended'), 60)}." if p.get("recommended") else ""))
        if a["status"] == "pending":
            lines.append(f"  Waiting for the owner ({_age_short(a['created_at'])}).")
        else:
            lines.append(f"  Answer ({_clock(a['decided_at'])}): {_one_line(a['result'], 400)}")
    if not qs:
        lines.append("No questions yet.")
    lines += ["", "## Owner decisions on requests"]
    decided = [a for a in st["approvals"] if a["action"] != "question" and a["decided_at"]][:200]
    for a in decided:
        verb = "rejected" if a["status"] == "rejected" else "approved"
        line = (f"- {_clock(a['decided_at'])} {verb} #{a['id']} {ACTION_LABELS.get(a['action'], a['action'])}: "
                f"{_one_line(a['summary'], 120)}")
        if a["feedback"]:
            line += f". Feedback: {_one_line(a['feedback'], 200)}"
        if a["status"] in ("done", "failed") and a["result"]:
            line += f". Outcome: {_one_line(a['result'], 160)}"
        lines.append(line)
    if not decided:
        lines.append("No decisions yet.")
    return "\n".join(lines) + "\n"


def status_text(company: dict) -> str:
    """records/STATUS.md content: the full facts block plus pending items, tasks and the latest requests."""
    return _status_text(_state(company))


def refresh_records(company: dict) -> None:
    """Rewrite records/ from the database (start of every run, and soon after every outbound action or decision).
    Never raises."""
    try:
        cid = company["id"]
        root = workspace.root(company)
        if not root.exists():
            return   # the company folder was removed; never recreate it from here
        with _records_lock:
            st = _state(company)
            rec = root / "records"
            _write_atomic(rec / "STATUS.md", _status_text(st))
            _write_atomic(rec / "outbound.md", _outbound_text(st))
            _write_atomic(rec / "emails.md", _emails_text(st))
            _write_atomic(rec / "owner.md", _owner_text(st))
            _last_write[cid] = time.monotonic()
        if not db.company_settings(st["company"]).get("truth_scan_done"):
            scan_existing(st["company"])
    except Exception as e:
        _warn("records refresh", e)


def _flush(cid: int) -> None:
    with _timer_lock:
        _timers.pop(cid, None)
    try:
        c = db.get_company(cid)
        if c:
            refresh_records(c)
    except Exception as e:
        _warn("records refresh", e)


def records_changed(company: dict) -> None:
    """An outbound action or owner decision happened: records/ is rewritten soon, at most once per 2 seconds.

    A timer does the work so the caller (an approval, an agent's tool call) never waits for it. The short minimum
    delay matters too: actions.audit() announces the change just before it inserts its row. Never raises."""
    try:
        cid = company["id"]
        with _timer_lock:
            if cid in _timers:
                return   # a rewrite is already on its way and will see this change too
            since = time.monotonic() - _last_write.get(cid, 0.0)
            t = threading.Timer(max(0.3, DEBOUNCE_SECONDS - since), _flush, (cid,))
            t.daemon = True
            _timers[cid] = t
        t.start()
    except Exception as e:
        _warn("records scheduling", e)


# ---------------------------------------------------------------- claims gate

# Words that make a sentence a plan, a draft, a condition or a negation rather than a claim (design 2.4, plus the
# relatives a model uses). Deliberately broad: a claim missed here is still contradicted by the facts block, while
# a false refusal blocks honest work.
_QUALIFIERS = re.compile(
    r"\b(?:drafts?|drafted|drafting|plans?|planned|planning|will|won't|would|could|should|shall|must|might|once|"
    r"pending|queued|queue|waiting|awaiting|wait|if|unless|proposed|propose|proposal|to[- ]do|todo|not\s+yet|"
    r"next\s+steps?|need(?:s|ed)?\s+to|going\s+to|ready\s+to|aim(?:s|ing)?\s+to|goals?|intend(?:s|ed)?|"
    r"expect(?:s|ed)?|until|before|request(?:s|ed)?|for\s+approval|after\s+approval|when\s+approved|when\s+ready|"
    r"when\s+the\s+owner|ask(?:s|ed|ing)?\s+(?:the\s+)?owner|hope|want(?:s|ed)?|try(?:ing)?|examples?|e\.g|"
    r"hypothetical(?:ly)?|"
    r"simulat\w*|mock(?:ed|up)?|pretend|imagine|suppose|assum\w*|targets?|can\s+be|to\s+be|not|no|never|nothing|"
    r"none|nobody|neither|nor|without|haven't|hasn't|hadn't|didn't|doesn't|don't|isn't|aren't|wasn't|weren't|"
    r"cannot|can't|zero)\b", re.I)
# Only for the website families: the local preview is real and may be described as working.
_LOCAL = re.compile(r"\b(?:local(?:ly)?|localhost|preview|staging)\b", re.I)
# "Deployed the fix to site/index.html": a file was written, which is not a claim about the internet.
_TO_LOCAL_FILE = re.compile(r"\bto\s+(?:the\s+)?(?:site/|code/|docs/|[\w./-]+\.(?:html?|css|js|md)\b)", re.I)
# A section under a heading like "Plan" or "Next steps" describes intentions, not events.
_HEADING_EXEMPT = re.compile(
    r"\b(?:drafts?|plans?|planned|planning|next\s+steps?|to[- ]?do|todo|proposed|proposals?|ideas?|checklist|"
    r"templates?|examples?|instructions?|how\s+to|goals?|roadmap|backlog|options?|open\s+questions|questions|"
    r"risks?|assumptions?|hypothes[ie]s|scripts?|sequence|copy)\b", re.I)
# Research describes what other people did ("Roofers who signed up for Angi report...", "contractors replied on
# Reddit that...", "was contacted by Angi"). That is about the market, not this company, so the families about
# people (email, signups, replies, posts) skip such sentences.
_RESEARCH = re.compile(r"\b(?:sources?:|according\s+to|reported|survey(?:s|ed)?|stud(?:y|ies)|reviews?|forums?|"
                       r"competitors?|rivals?|industry|on\s+average)\b|\[fact\s*#?\d+\]", re.I)
_RELATIVE = re.compile(r"\b(?:who|that|which)\s+(?:have\s+|has\s+|had\s+|already\s+)?(?:signed\s+up|subscribed|joined|"
                       r"registered|enrolled|replied|responded|posted|contacted|e-?mailed|onboarded|recruited)\b", re.I)
_BY_OTHERS = re.compile(r"\b(?:was|were|been|being|get|got|gets)\s+(?:\w+\s+)?(?:contacted|e-?mailed|recruited|"
                        r"signed\s+up|subscribed|onboarded|posted|reached)\s+(?:out\s+to\s+)?by\s+"
                        r"(?!us\b|our\b|me\b|holdco\b|the\s+company\b)", re.I)
_REPLIED_ELSEWHERE = re.compile(r"\b(?:replied|responded)\s+(?:that\b|(?:on|in|to)\s+(?!(?:our|my|us|me)\b))", re.I)
# Events before the company existed are about the world, whatever the verb.
_OLD_TIMES = re.compile(r"\b(?:last\s+year|years\s+ago|in\s+the\s+past|historically)\b", re.I)
_PEOPLE_FAMILIES = ("email", "signups", "replies", "posted")

_EMAIL_WORDS = r"\b(?:e-?mails?|outreach|newsletters?|follow[- ]?ups?|pitch(?:es)?|cold)\b|@\w"
_FAMILIES: dict[str, list[tuple[re.Pattern, re.Pattern | None, re.Pattern | None]]] = {}


def _fam(name: str, pattern: str, context: str | None = None, exclude: str | None = None) -> None:
    """One way of stating a claim: the pattern, words that must also be in the sentence, words that cancel it."""
    _FAMILIES.setdefault(name, []).append((re.compile(pattern, re.I), re.compile(context, re.I) if context else None,
                                           re.compile(exclude, re.I) if exclude else None))


# A sentence that starts with the verb ("Deployed the site.", "- Posted: launch thread") has the writer as its
# subject, the way agents write summaries; "Roofr deployed a feature" or "users posted reviews" do not.
_START = r"^(?:[-*+]\s+)?(?:[\w ]{1,24}:\s+)?"
_US = r"\b(?:we|i|holdco|we've|i've)\s+(?:have\s+|has\s+|had\s+|just\s+|already\s+|now\s+)*"
_SITE = r"(?:site|website|web\s*site|landing\s+page|page|homepage|it|app|form|changes|update|fix|new\s+version)"
_fam("deployed", _START + r"(?:re)?deployed\b")
_fam("deployed", _US + r"(?:re)?deployed\b")
_fam("deployed", r"\b" + _SITE + r"\s+(?:is|are|was|were|has\s+been|have\s+been|got)\s+(?:now\s+|successfully\s+)?"
                 r"(?:re)?deployed\b")
_fam("deployed", r"\b(?:re)?deployed\s+(?:the\s+|our\s+)?(?:site|website|landing\s+page|page|homepage|app|form|"
                 r"changes|update|fix|new\s+version)\b")
_fam("deployed", r"\bdeployed\s+(?:it\s+)?(?:to|on)\s+(?:netlify|vercel|production|the\s+web|the\s+internet|https?://)")
_fam("deployed", r"\bdeployment\s+(?:is\s+|was\s+)?(?:confirmed|complete[d]?|successful|succeeded|done|live)\b")
_fam("deployed", r"\b(?:site|website|web\s*site|landing\s+page|page|homepage|it|app|form|url|link|domain|we|everything)"
                 r"\s+(?:is\s+|are\s+|was\s+|went\s+|has\s+gone\s+|have\s+gone\s+)(?:now\s+|officially\s+)?live\b")
_fam("deployed", r"\b(?:we're|it's|we\s+are|we\s+went|now)\s+live\b")
_fam("deployed", r"\blive\s+(?:url|link|site|website|address)\b")
_fam("deployed", r"\blive\s+(?:at|on)\s+(?:https?://|www\.|netlify|vercel|[\w-]+\.(?:com|co|net|org|io|app|dev|site)\b)")
_fam("deployed", r"\bpublished\s+(?:the\s+|our\s+)?(?:site|website|landing\s+page|web\s*page|page)\b")
_fam("deployed", r"\b(?:site|website|landing\s+page|page)\s+(?:is|was|has\s+been|went)\s+(?:now\s+)?"
                 r"(?:published|online|launched|up\s+and\s+running|up\s+(?:at|on)\b)")
_fam("deployed", r"\blaunched\s+(?:the\s+|our\s+)?(?:site|website|landing\s+page)\b")
_fam("email", r"\be-?mailed\b")
_fam("email", r"\breached\s+out\s+to\b")
_fam("email", r"\bcontacted\b")
_fam("email", r"\bsent\b", _EMAIL_WORDS)
_fam("email", r"\bdelivered\b", r"\b(?:e-?mails?|newsletters?|inboxes)\b|\bto\s+(?:their|the|your)\s+inbox\b")
_fam("email", r"\b(?:e-?mails?|outreach)\s+(?:went\s+out|(?:is\s+|was\s+|has\s+been\s+)?(?:done|complete[d]?|"
              r"launched))\b")
_fam("signups", r"\bsigned\s+up\b")
_fam("signups", r"\bsubscribed\b")
_fam("signups", r"\brecruited\b")
_fam("signups", r"\bonboarded\b")
_fam("signups", r"\benrolled\b")
_fam("signups", r"\bregistered\s+for\b", None, r"\b(?:we|i)\s+(?:have\s+|had\s+|just\s+)?registered\s+for\b")
_fam("signups", r"\bjoined\b", r"\b(?:wait\s?list|list|newsletter|network|platform|beta|program(?:me)?|directory|"
                               r"community|marketplace|pilot)\b|\b\d+\s+\w+(?:\s+\w+)?\s+(?:have\s+|has\s+)?joined\b",
     r"\bjoined\s+(?:the\s+)?team\b")
_fam("signups", r"\b(?:got|received|have|has|had)\s+\d[\d,]*\s+(?:new\s+)?(?:sign-?ups|subscribers|members|users|"
                r"customers|paying\s+customers)\b")
_fam("signups", r"\b\d[\d,]*\s+(?:new\s+)?sign-?ups\b")
_fam("form", r"\btest\s+submissions?\b")
_fam("form", r"\bsubmitted\b", r"\b(?:form|wait\s?list|netlify|entry|test)\b")
_fam("form", r"\bform\s+(?:works|is\s+working|worked|was\s+tested|has\s+been\s+tested|tested)\b")
_fam("form", r"\bsubmissions?\s+(?:received|recorded|came\s+in|confirmed|logged|captured)\b")
_fam("form", r"\b(?:tested|verified|confirmed)\s+(?:the\s+)?(?:live\s+)?(?:wait\s?list\s+|contact\s+|sign-?up\s+)?"
             r"form\b")
_fam("posted", _START + r"posted\b")
_fam("posted", _US + r"posted\b")
_fam("posted", r"\bposted\s+(?:the|our|a|an|\d+)\s+(?:\w+\s+){0,2}(?:posts?|tweets?|threads?|announcements?|updates?|"
               r"videos?|articles?|reels?|stor(?:y|ies))\b")
_fam("posted", r"\b(?:posts?|tweets?|threads?|announcements?|updates?)\s+(?:is|are|was|were|has\s+been|have\s+been|"
               r"went)\s+(?:now\s+)?(?:posted|published|live|out)\b")
_fam("posted", _START + r"tweeted\b")
_fam("posted", _US + r"(?:tweeted|shared\s+(?:it|the\s+\w+|our\s+\w+)\s+on)\b")
_fam("posted", r"\b(?:posts?|tweets?|threads?|announcements?)\s+(?:posted|published)\b")
_fam("posted", r"\bpublished\s+(?:a\s+|the\s+|our\s+)?(?:post|tweet|thread)s?\b")
_fam("posted", r"\bwent\s+out\s+on\s+(?:x|twitter|linkedin|reddit|facebook|instagram|threads|bluesky)\b")
_NOT_MONEY = (r"(?!\s+(?:close\s+)?(?:attention|off|heed|tribute|respect|a\s+visit|ads?|advertising|plans?|tiers?|"
              r"search|social|media|traffic|versions?|users?|customers?|subscribers?|subscriptions?|newsletters?|"
              r"options?|products?|courses?|campaigns?|channels?|placements?|listings?|memberships?|acquisition|"
              r"accounts?|leads?|features?|apps?|tools?|members?|content|promotions?)\b)")
_fam("purchase", _US + r"(?:paid|purchased|bought)\b" + _NOT_MONEY)
_fam("purchase", _START + r"(?:paid|purchased|bought)\b" + _NOT_MONEY)
_fam("purchase", _US + r"spent\s+(?:\$|£|€|\d[\d,.]*\s*(?:usd|dollars|euros|pounds))")
_fam("purchase", r"\bpaid\s+for\s+(?:the|a|an|our)\b")
_fam("purchase", r"\b(?:purchased|bought)\s+(?:the\s+|a\s+|an\s+|our\s+)?(?:domain|plan|subscription|tool|licen[cs]e|"
                 r"template|theme|ads?|list|leads|account)\b")
_fam("purchase", r"\bregistered\s+(?:the\s+|a\s+|our\s+)?(?:domain|[a-z0-9-]+\.(?:com|co|net|org|io|app|dev|ai|uk|"
                 r"us))\b")
_fam("purchase", r"\bformed\s+(?:the\s+|an\s+|a\s+|our\s+)?(?:llc|company|corporation|corp|ltd|business\s+entity)\b")
_fam("purchase", _US + r"incorporated\b")
_fam("purchase", r"\bdomain\s+(?:is\s+|was\s+|has\s+been\s+)?(?:registered|purchased|bought)\b")
_fam("purchase", r"\b(?:purchase|payment)\s+(?:is\s+|was\s+|has\s+been\s+)?(?:complete[d]?|confirmed|made|done)\b")
_fam("replies", r"\b(?:\d+|one|two|three|several|some|many|they|he|she|prospects?|leads?|roofers?|customers?|"
                r"contractors?|providers?|people|recipients?)\s+(?:have\s+|has\s+)?(?:replied|responded)\b")
_fam("replies", r"\b(?:got|received|have|had)\s+(?:\d+\s+|a\s+|some\s+|several\s+)?(?:new\s+)?(?:replies|responses|"
                r"reply|response)\b", None, r"\b(?:replies|responses|reply|response)\s+from\s+(?:the\s+)?owner\b")
_fam("replies", r"\breplied\s+to\s+(?:our|my|the)\s+(?:e-?mail|outreach|message)\b")
_fam("replies", r"\bbooked\s+(?:a\s+|the\s+|\d+\s+)?(?:call|calls|meeting|meetings|demo|demos)\b")
_fam("replies", r"\bscheduled\s+(?:a\s+|\d+\s+)?(?:call|calls|meeting|meetings|demo|demos)\s+with\b")

FAMILY_DESC = {
    "deployed": "the site is deployed or live",
    "email": "email was sent",
    "signups": "people signed up, joined or were recruited",
    "form": "the form was tested on the live site",
    "posted": "something was posted",
    "purchase": "something was paid for, bought or registered",
    "replies": "someone replied or booked a call",
    "date": "something happened on a date that has not come yet",
}

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "fifty": 50, "hundred": 100, "dozen": 12}
_NUM = r"(\d[\d,]*|" + "|".join(_NUMWORDS) + r")"
_EMAIL_COUNT = [re.compile(r"\b(?:sent|e-?mailed|contacted|reached\s+out\s+to)\s+(?:out\s+)?(?:a\s+total\s+of\s+|"
                           r"about\s+|around\s+|over\s+|all\s+)?" + _NUM + r"\b", re.I),
                re.compile(r"\b" + _NUM + r"\s+(?:\w+\s+){0,2}(?:e-?mails|messages)\b", re.I)]
_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+", re.I)
_BARE_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:netlify\.app|vercel\.app|pages\.dev|github\.io|"
                           r"herokuapp\.com)\b", re.I)
_PAST = re.compile(r"\b(?:was|were|did|had|completed|finished|done|held|met|called|reviewed|updated|launched|created|"
                   r"wrote|written|built|ran|confirmed|received|signed|agreed|decided|approved|happened|occurred)\b",
                   re.I)
_DATE_QUALIFIERS = re.compile(r"\b(?:due|deadline|by|scheduled|schedule|target|starts?|starting|from)\b", re.I)


def _to_int(s: str) -> int:
    s = s.lower().replace(",", "")
    return _NUMWORDS[s] if s in _NUMWORDS else int(s)


def _dates_in(text: str) -> list[_dt.date]:
    out = []
    for m in re.finditer(r"(?<!\d)(20\d{2})[-_/](0?[1-9]|1[0-2])[-_/](0?[1-9]|[12]\d|3[01])(?!\d)", text):
        try:
            out.append(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    months = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
    months.update({m[:3].lower(): i + 1 for i, m in enumerate(_MONTHS)})
    mon = r"(" + "|".join(sorted(months, key=len, reverse=True)) + r")\.?"
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + mon + r",?\s+(20\d{2})\b", text, re.I):
        try:
            out.append(_dt.date(int(m.group(3)), months[m.group(2).lower()], int(m.group(1))))
        except (ValueError, KeyError):
            pass
    for m in re.finditer(r"\b" + mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", text, re.I):
        try:
            out.append(_dt.date(int(m.group(3)), months[m.group(1).lower()], int(m.group(2))))
        except (ValueError, KeyError):
            pass
    return out


def _families_in(sentence: str) -> list[str]:
    found = []
    for fam, pats in _FAMILIES.items():
        for rx, ctx_rx, ex_rx in pats:
            if rx.search(sentence) and (ctx_rx is None or ctx_rx.search(sentence)) and \
                    (ex_rx is None or not ex_rx.search(sentence)):
                found.append(fam)
                break
    return found


def _pending_ids(st: dict, action: str) -> list[int]:
    return [a["id"] for a in st["pending"] if a["action"] == action]


def _judge(st: dict, fam: str, sentence: str) -> tuple[bool, str, str]:
    """(supported, what the records say, suggested rewrite) for one claim family in one sentence."""
    if fam == "deployed":
        pend = _pending_ids(st, "deploy_site")
        if not st["website"]["deployed"]:
            waiting = f" (deploy request #{pend[0]} is waiting for the owner)" if pend else ""
            rewrite = ("Write 'ready to deploy, waiting for approval' instead." if pend else
                       "Write 'built and checked in the local preview; not deployed yet' instead.")
            return False, f"no deploy has run{waiting}", rewrite
        stated = {_host(u.rstrip(".,;:!?)")) for u in _URL_RE.findall(sentence)} | \
            {h.lower() for h in _BARE_HOST_RE.findall(sentence)}
        wrong = [h for h in stated if h and h not in st["deploy_hosts"]]
        if wrong:
            real = st["website"]["url"] or "its deploy address"
            return False, f"the site was deployed to {real}, not {wrong[0]}", f"Use {real} or remove the link."
        return True, "", ""
    if fam == "email":
        em = st["email"]
        n = em["sent_total"]
        pend = _pending_ids(st, "send_email")
        counts = [_to_int(m.group(1)) for rx in _EMAIL_COUNT for m in rx.finditer(sentence)]
        if counts and max(counts) == 0:
            return True, "", ""   # "emailed 0 of 20" is the honest number
        if n == 0:
            why = "no email has been sent" + ("" if em["connected"] else " (email is not connected)")
            if pend:
                why += f"; {_plural(len(pend), 'email request')} are queued for the owner"
            rewrite = (f"Write '{_plural(len(pend), 'email')} queued for the owner's approval' instead." if pend
                       else "Write 'drafted, not sent' instead.")
            return False, why, rewrite
        if counts and max(counts) > n:
            return False, f"Holdco has sent {_plural(n, 'email')}, not {max(counts)}", f"Use {n}, the real number."
        missing = [a for a in _ADDR_RE.findall(sentence) if a.lower() not in st["out_addrs"]]
        if missing:
            return False, f"no email to {missing[0]} has been sent", "Remove that address or say it was not contacted."
        return True, "", ""
    if fam == "signups":
        if st["signup_ok"]:
            return True, "", ""
        return (False, "no signups, subscribers or recruits are recorded from a real source (forms and payments are "
                "not connected yet)", "Write 'no signups yet' or 'N prospects identified, not contacted' instead.")
    if fam == "form":
        if st["website"]["deployed"] and st["form_ok"]:
            return True, "", ""
        why = ("the site is not deployed and no form submission is recorded" if not st["website"]["deployed"] else
               "no form submission is recorded by a connector")
        return False, why, "Write 'form checked in the local preview only' instead."
    if fam == "posted":
        if st["social"]["sent"]:
            return True, "", ""
        pend = _pending_ids(st, "social_post")
        if pend:
            return (False, f"no post has gone out; post request #{pend[0]} is waiting for the owner",
                    f"Write 'post queued for the owner's approval (request #{pend[0]})' instead.")
        if st["manual_posts"]:
            a = st["manual_posts"][0]
            return (False, f"post #{a['id']} was handed to the owner to post by hand and Holdco has no record it went "
                    "out", "Write 'handed to the owner to post' instead.")
        return False, "no post has been published", "Write 'post drafted, not published' instead."
    if fam == "purchase":
        if st["money"]["purchases"]:
            return True, "", ""
        pend = _pending_ids(st, "spend_money")
        if pend:
            return (False, f"no purchase has been approved (purchase request #{pend[0]} is waiting for the owner)",
                    f"Write 'purchase requested, waiting for the owner (request #{pend[0]})' instead.")
        return False, "no purchase has been approved", "Write 'not bought; purchases need the owner' instead."
    if fam == "replies":
        if not st["email"]["replies"]:
            return False, "no reply is recorded (Holdco has no inbound email)", "Write 'no replies yet' instead."
        missing = [a for a in _ADDR_RE.findall(sentence) if a.lower() not in st["in_addrs"]]
        if missing:
            return False, f"no email from {missing[0]} has arrived", "Remove that claim or say no reply came."
        return True, "", ""
    return True, "", ""


def _draft_path(path: str) -> bool:
    """Copy kept to publish later (drafts/, templates/, or 'draft' in the name) is not a record of events."""
    parts = re.split(r"[/_\-.\s]+", (path or "").lower())
    return any(p in ("draft", "drafts", "template", "templates") for p in parts)


def _strip_quotes(line: str) -> str:
    """Quoted text and inline code are someone's words or an instruction, not the writer's claim."""
    line = re.sub(r"`[^`]*`", " ", line)
    line = re.sub(r"\"[^\"\n]{0,400}\"|“[^”\n]{0,400}”|‘[^’\n]{0,400}’", " ", line)
    # 'single quotes' too, but not apostrophes: the opening quote must not follow a letter (Bob's, roofers')
    return re.sub(r"(?<![\w'])'[^'\n]{1,300}'(?![\w'])", " ", line)


_HOSTING = (".netlify.app", ".vercel.app", ".pages.dev", ".github.io", ".herokuapp.com")
_INSIDERS = r"(?:the\s+)?(?:owner|ceo|team|teammates?|qa|engineering|marketing|sales|research|ops|support)"


def _ours(host: str, info: dict) -> bool:
    """Could this host be the company's own site? Its Settings address, a hosting address, or its name in it."""
    h = host.lower()
    if not h:
        return False
    if h == info["site_host"] or h.endswith(_HOSTING):
        return True
    flat = h.replace("-", "")
    return any(t in flat for t in info["name_tokens"])


def _about_others(fam: str, sentence: str, info: dict) -> bool:
    """True when the sentence is about someone else, a teammate hand-off or a time before the company existed, so
    it is not this company's claim about the outside world."""
    low = sentence.lower()
    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", sentence)]
    if _OLD_TIMES.search(sentence) or (years and max(years) < info["year"]):
        return True
    if fam in _PEOPLE_FAMILIES and (_RESEARCH.search(sentence) or _RELATIVE.search(sentence)
                                    or _BY_OTHERS.search(sentence)):
        return True
    if fam == "replies" and _REPLIED_ELSEWHERE.search(sentence):
        return True
    if fam in ("deployed", "posted") and _TO_LOCAL_FILE.search(sentence):
        return True   # "Deployed the fix to site/index.html", "Posted findings to research/x.md"
    if fam == "deployed":
        m = re.search(r"\b([\w-]+)'s\s+(?:new\s+)?(?:site|website|landing\s+page|page|homepage|app)\b", low)
        if m and m.group(1) not in ("our", "my") and not any(t in m.group(1).replace("-", "")
                                                             for t in info["name_tokens"]):
            return True   # "Roofr's pricing page is live"
        hosts = {_host(u.rstrip(".,;:!?)")) for u in _URL_RE.findall(sentence)} | \
            {_host(h) for h in re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|co|net|org|io|app|dev|ai|uk|us|site)\b", low)}
        about_ours = re.search(r"\b(?:our|the)\s+(?:new\s+)?(?:site|website|landing\s+page|homepage)\b|"
                               r"\bwe(?:'re|\s+are|\s+went)\s+live\b", low)
        if hosts and not about_ours and not any(_ours(h, info) for h in hosts):
            return True   # someone else's live site, e.g. "Roofr is live at roofr.com"
    if fam == "email":
        # Handing work to a teammate or the owner is not outreach ("Sent the email copy to Atlas").
        insider = re.search(rf"\b(?:to|contacted|e-?mailed|reached\s+out\s+to)\s+{_INSIDERS}\b", low) or \
            any(re.search(rf"\b(?:to|contacted|e-?mailed)\s+{re.escape(n)}\b", low) for n in info["team"])
        if insider and "@" not in low:
            return True
    return False


def _claim_info(company: dict) -> dict:
    created = _local(company.get("created_at"))
    name = (company.get("name") or "").strip()
    cs = db.company_settings(company)
    tokens = {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 4}
    if name:
        tokens.add(re.sub(r"[^a-z0-9]", "", name.lower()))
    return {"year": created.year if created else _today().year,
            "team": {(a["name"] or "").lower() for a in db.q("SELECT name FROM agents WHERE company_id=?",
                                                             (company["id"],)) if a["name"]},
            "name_tokens": {t for t in tokens if len(t) >= 4},
            "site_host": _host(cs.get("site_url") or ""),
            "name_live": re.compile(rf"\b{re.escape(name)}\s+(?:is|went|has\s+gone)\s+(?:now\s+|officially\s+)?live\b",
                                    re.I) if name else None}


def check_claims(company: dict, text: str, path: str = "", skip_lines: set | None = None) -> list[dict]:
    """Sentences in `text` that state a real-world event the records do not support (design 2.4).

    Each problem is a dict: line, sentence, family, records (what Holdco's records say), rewrite (a suggestion)
    and text (all of it in one line for the agent). Plans, drafts, questions, negations, quoted text, code blocks,
    sections under a plan-like heading and files in a drafts/ folder are not claims. Lines in skip_lines (stripped)
    are left alone: write_guard passes the lines a file already had, so only new text is judged."""
    if not text or not text.strip() or _draft_path(path):
        return []
    if path.lower().endswith((".html", ".htm")):
        try:
            from . import web
            text = web.html_to_text(text)[1]
        except Exception:
            pass
    st = None
    problems: list[dict] = []
    seen: set = set()
    in_fence = False
    stack: list[tuple[int, bool]] = []
    today = _today()
    info = _claim_info(company)
    name_live = info["name_live"]
    lines = text.replace("’", "'").splitlines()
    for no, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not s:
            continue
        hm = re.match(r"^(#{1,6})\s+(.*)$", s)
        if hm:
            level = len(hm.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, bool(_HEADING_EXEMPT.search(hm.group(2)))))
            s = hm.group(2)
        if any(ex for _, ex in stack):
            continue
        if s.startswith(">") or (skip_lines and raw.strip() in skip_lines):
            continue
        if s.startswith("|"):
            # A table row: each cell is read on its own ("| Bob | Recruited |"); a header row (the next line is
            # |---|) only names columns, and a separator row says nothing.
            nxt = lines[no].strip() if no < len(lines) else ""
            if re.match(r"^\|?[\s:|-]+\|?$", s) or re.match(r"^\|?[\s:|-]+\|?$", nxt) and "-" in nxt:
                continue
            s = ". ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        for sentence in re.split(r"(?<=[.!?;])\s+", _strip_quotes(s)):
            sentence = sentence.strip()
            if not sentence or "?" in sentence or _QUALIFIERS.search(sentence):
                continue
            fams = _families_in(sentence)
            if name_live and "deployed" not in fams and name_live.search(sentence):
                fams.append("deployed")
            fams = [f for f in fams if not _about_others(f, sentence, info)]
            future = [d for d in _dates_in(sentence) if d > today]
            if future and (fams or _PAST.search(sentence)) and not _DATE_QUALIFIERS.search(sentence):
                if (no, "date") not in seen:
                    seen.add((no, "date"))
                    problems.append(_problem(no, sentence, "date", f"that date ({_long_date(future[0])}) has not "
                                             f"come yet; today is {_long_date(today)}",
                                             "Use the real date, or describe it as planned."))
                continue
            for fam in fams:
                if (fam in ("deployed", "form") and _LOCAL.search(sentence)) or (no, fam) in seen:
                    continue
                if st is None:
                    st = _state(company)
                ok, records, rewrite = _judge(st, fam, sentence)
                if not ok:
                    seen.add((no, fam))
                    problems.append(_problem(no, sentence, fam, records, rewrite))
    return problems


def _problem(no: int, sentence: str, fam: str, records: str, rewrite: str) -> dict:
    snippet = _one_line(sentence, 140)
    return {"line": no, "sentence": snippet, "family": fam, "records": records, "rewrite": rewrite,
            "text": f"Line {no}: \"{snippet}\" says {FAMILY_DESC[fam]}, but {records}. {rewrite}"}


def _list(problems: list[dict], n: int = 8) -> str:
    out = "\n".join(f"- {p['text']}" for p in problems[:n])
    if len(problems) > n:
        out += f"\n- ...and {len(problems) - n} more."
    return out


def _refused(ctx: Ctx, where: str, rule: str, first: str, **data) -> None:
    """Every refusal is counted (event kind 'claims') so the Bench can score how often a model tries."""
    try:
        bus.emit(ctx.company["id"], "claims", f"refused {where} ({rule}): {_one_line(first, 220)}",
                 (ctx.agent or {}).get("id"), {"where": where, "rule": rule, "problem": _one_line(first, 600), **data})
    except Exception as e:
        _warn("claims event", e)


# ---------------------------------------------------------------- guards

_LOG_PARTS = {"log", "logs", "confirmation", "confirmations", "receipt", "receipts", "sent", "submission",
              "submissions"}
_TEXT_EXT = {"", ".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".rtf",
             ".html", ".htm"}
_FILE_DATE = re.compile(r"(?<!\d)(20\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])(?!\d)")
RECORDS_REFUSAL = "ERROR: records/ is written by Holdco from what really happened. Write plans and notes elsewhere."
CONTEXT_REFUSAL = "ERROR: context/ is the owner's. Save your notes elsewhere, for example docs/."
LOG_REFUSAL = ("ERROR: Holdco keeps the real record of what happened in records/. If this is a plan, name it as a "
               "plan, for example docs/outreach-plan.md.")


def _log_like(rel: str) -> bool:
    """Names like email_log.txt or deployment_confirmation.md (design 2.2), in a text format."""
    p = Path(rel)
    ext = p.suffix.lower()
    if ext not in _TEXT_EXT:
        return False
    if ext == ".log":
        return True
    stem = re.sub(r"([a-z])([A-Z])", r"\1_\2", p.stem)   # emailLog -> email_Log
    return any(part in _LOG_PARTS for part in re.split(r"[_\-.\s]+", stem.lower()))


def future_date_in(rel: str) -> _dt.date | None:
    """A YYYY-MM-DD or YYYY_MM_DD date in a path that is after today's local date (design 2.8)."""
    today = _today()
    for m in _FILE_DATE.finditer(rel or ""):
        try:
            d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d > today:
            return d
    return None


def write_guard(ctx: Ctx, rel_path: str, content: str, is_edit: bool = False) -> str | None:
    """Before write_file/edit_file saves: records/ and context/ protection, future dates in names, log-like names
    for new files, unsupported claims in the new lines of files outside code/ and site/. Returns the refusal."""
    try:
        where = "edit_file" if is_edit else "write_file"
        rel = re.sub(r"^(?:\./)+", "", (rel_path or "").replace("\\", "/")).strip("/")
        first = rel.lower().split("/", 1)[0]
        if first == "records":
            _refused(ctx, where, "records", RECORDS_REFUSAL, path=rel)
            return RECORDS_REFUSAL
        if first == "context":
            _refused(ctx, where, "context", CONTEXT_REFUSAL, path=rel)
            return CONTEXT_REFUSAL
        if future_date_in(rel):
            msg = (f"ERROR: that date is in the future; today is {_long_date(_today())}. Name the file with today's "
                   "date or without a date.")
            _refused(ctx, where, "future_date", msg, path=rel)
            return msg
        if first in ("code", "site"):
            return None   # code and the website are not records of events; deploy_guard checks the site
        try:
            target = workspace.safe_path(ctx.company, rel)
            existed = target.is_file()
        except Exception:
            target, existed = None, False
        if not existed and _log_like(rel):
            # The claims are listed too: the agent learns what really happened, not only that the name is wrong.
            problems = check_claims(ctx.company, content, rel)
            _refused(ctx, where, "log_name", LOG_REFUSAL, path=rel, count=len(problems))
            if problems:
                return (f"{LOG_REFUSAL}\nIt also states things Holdco's records do not show:\n{_list(problems, 5)}")
            return LOG_REFUSAL
        old_lines: set = set()
        if existed and target is not None:
            try:
                old_lines = {x.strip() for x in target.read_text(encoding="utf-8", errors="replace").splitlines()
                             if x.strip()}
            except OSError:
                pass
        problems = check_claims(ctx.company, content, rel, old_lines)
        if problems:
            _refused(ctx, where, "claims", problems[0]["text"], path=rel, count=len(problems))
            return (f"ERROR: not saved: {rel} states things Holdco's records do not show:\n{_list(problems)}\n"
                    "Fix those lines (or describe them as planned or waiting), then save again. The Facts block and "
                    "records/ say what has really happened. Copy to publish later (a post or email draft) belongs in "
                    "a drafts/ folder.")
    except Exception as e:
        _warn("write guard", e)
    return None


def claims_guard(ctx: Ctx, text: str, where: str) -> str | None:
    """Before report_to_owner / ask_owner sends text to the owner (where = 'report' | 'ask')."""
    try:
        problems = check_claims(ctx.company, text or "")
        if problems:
            _refused(ctx, where, "claims", problems[0]["text"], count=len(problems))
            return ("ERROR: not sent to the owner: the message states things Holdco's records do not show:\n"
                    f"{_list(problems)}\nRewrite those sentences and send again. The Facts block says what has "
                    "really happened.")
    except Exception as e:
        _warn("claims guard", e)
    return None


ACTION_KINDS = {"outreach": "send_email", "deploy": "deploy_site", "post": "social_post", "purchase": "spend_money",
                "ask": "question"}
_KIND_NOUN = {"outreach": "email", "deploy": "deploy", "post": "post", "purchase": "purchase", "ask": "question"}
_KIND_TOOL = {"outreach": "send_email", "deploy": "deploy_site", "post": "social_post", "purchase": "request_purchase",
              "ask": "ask_owner"}


def _approval_done(a: dict) -> bool:
    if a["action"] == "question":
        return a["status"] == "answered"
    if a["status"] != "done":
        return False
    return _deploy_ok(a["result"]) if a["action"] == "deploy_site" else True


def _audit_done(action: str, result: str) -> bool:
    r = str(result or "")
    if r.startswith("FAILED"):
        return False
    if action == "deploy_site":
        return _deploy_ok(r)
    if action == "send_email":
        return r.startswith("Email sent")
    return True


def action_state(ctx: Ctx, kind: str) -> str:
    """'done' when this task's action really happened, 'waiting' while it is queued for the owner, otherwise
    'missing'. tools.finish can use it to end a task 'waiting' instead of 'partial'."""
    task = ctx.task or {}
    action = ACTION_KINDS.get(kind)
    if not action or not task.get("id"):
        return "missing"
    cid = ctx.company["id"]
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND task_id=? AND action=? ORDER BY id DESC",
                (cid, task["id"], action))
    if any(_approval_done(a) for a in rows):
        return "done"
    # A policy set to automatic runs the action at once and leaves only an audit row (no approval, no task id).
    since = db.scalar("SELECT MIN(started_at) FROM runs WHERE task_id=?", (task["id"],)) or task.get("created_at") or ""
    for a in db.q("SELECT result FROM audit WHERE company_id=? AND action=? AND approval_id IS NULL AND proposed_by=? "
                  "AND created_at>=?", (cid, action, (ctx.agent or {}).get("name", ""), since)):
        if _audit_done(action, a["result"]):
            return "done"
    return "waiting" if any(a["status"] in ("pending", "running") for a in rows) else "missing"


def _action_problem(ctx: Ctx, kind: str, deliverable: str) -> str | None:
    """design 2.5: an action task is done only when the action really happened, never because of a file."""
    state = action_state(ctx, kind)
    if state == "done":
        return None
    task = ctx.task or {}
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND task_id=? AND action=? ORDER BY id DESC",
                (ctx.company["id"], task.get("id"), ACTION_KINDS[kind]))
    noun = _KIND_NOUN[kind]
    article = "an" if kind[0] in "aeiou" else "a"
    if state == "waiting":
        ids = ", ".join(f"#{a['id']}" for a in rows if a["status"] in ("pending", "running"))
        return (f"this is {article} {kind} task, so it is done only when the {noun} has really happened, and request "
                f"{ids} is still waiting for the owner. Call finish again with status 'waiting'; the task reopens "
                "when the owner decides.")
    latest = rows[0] if rows else None
    if latest and latest["action"] == "deploy_site" and latest["status"] == "done":
        return (f"the owner approved deploy request #{latest['id']}, but no Netlify token is set, so the site is not "
                "online. Finish with status 'blocked' and say the owner needs to add a Netlify token in the company's "
                "Settings.")
    if latest and latest["status"] == "failed":
        return (f"request #{latest['id']} failed ({_one_line(latest['result'], 140)}). Finish with status 'blocked' "
                "and say what is needed.")
    if latest and latest["status"] == "rejected":
        return (f"the owner rejected request #{latest['id']}. Revise it as they asked and submit it once more, or "
                "finish with status 'blocked'.")
    d = (deliverable or "").strip()
    file_note = (f" A file such as {d} does not count." if d and ("/" in d or "." in d) and " " not in d
                 else " A file does not count.")
    extra = ""
    if kind == "outreach" and not emailer.smtp_configured(db.company_settings(db.get_company(ctx.company["id"])
                                                                              or ctx.company)):
        extra = (" Email is not connected; this task cannot run yet. Ask the owner for a mailbox once, or finish "
                 "with status 'blocked'.")
    return (f"this is {article} {kind} task: it is done only when the {noun} really happens, and no {noun} has been "
            f"queued in this task.{file_note} Call {_KIND_TOOL[kind]} (then finish with status 'waiting'), or finish "
            f"with status 'blocked' and say why.{extra}")


def finish_guard(ctx: Ctx, status: str, deliverable: str, summary: str, evidence: str) -> str | None:
    """Before finish(status='done') is accepted: unsupported claims in the summary and evidence, the action-task
    rule (design 2.5) and the source check for research deliverables (design 5.3). Returns the problem text."""
    try:
        if (status or "done") != "done":
            return None
        parts: list[str] = []
        rule = ""
        claims = check_claims(ctx.company, "\n".join(x for x in (summary or "", evidence or "") if x))
        if claims:
            parts.append("your summary states things Holdco's records do not show:\n" + _list(claims, 5))
            rule = "claims"
        task = ctx.task
        if task:
            kind = (task.get("kind") or "").strip().lower() or \
                classify_task(task.get("title") or "", "", task.get("deliverable") or "")
            if kind in ACTION_KINDS:
                p = _action_problem(ctx, kind, deliverable)
                if p:
                    parts.append(p)
                    rule = rule or "action_task"
        d = (deliverable or "").strip().strip("`'\"")
        if d and (d.lower().startswith("research/") or d.lower() == "docs/charter.md"):
            try:
                p = workspace.safe_path(ctx.company, d)
                bad = source_problems(ctx.company, p.read_text(encoding="utf-8", errors="replace")) \
                    if p.is_file() else []
            except Exception:
                bad = []
            if bad:
                shown = "; ".join(f"line {n}: {_one_line(t, 80)}" for n, t, _ in bad[:5])
                more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
                parts.append(f"{d} has {_plural(len(bad), 'line')} with numbers and no source: {shown}{more}. Add the "
                             "URL you fetched or a [fact N] reference, label estimates as 'estimate', or remove the "
                             "numbers (check_sources lists them).")
                rule = rule or "sources"
        if not parts:
            return None
        _refused(ctx, "finish", rule, parts[0], task_id=(task or {}).get("id"))
        return "\n".join(parts)
    except Exception as e:
        _warn("finish guard", e)
        return None


# ---------------------------------------------------------------- outreach

_RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost", ".local")
_PLACEHOLDER_LOCALS = {"you", "your", "name", "user", "someone", "test", "email", "foo", "bar", "abc", "xxx",
                       "john.doe", "jane.doe", "johndoe", "janedoe", "firstname", "first.last", "firstname.lastname"}
_PLACEHOLDER_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|domain\.com|yourdomain\.\w+|mydomain\.\w+|"
                                  r"yourcompany\.\w+|company\.com|website\.com)$")
STOP_WORDS = re.compile(r"\b(?:stop|unsubscribe|remove\s+me)\b", re.I)
OPT_OUT = "Reply 'stop' and we won't email you again."
_OPT_OUT_RE = re.compile(r"\b(?:stop|unsubscribe|opt[ -]?out)\b|won'?t\s+(?:e-?mail|follow\s+up|contact|reach\s+out)|"
                         r"reply\s+.{0,12}\bno\b", re.I)


def _own_words(body: str) -> str:
    """The part of a reply its sender wrote: quoted lines and the original below 'On ... wrote:' are cut, because
    our own opt-out line ('Reply stop...') comes back quoted in every reply."""
    out = []
    for line in (body or "").splitlines():
        s = line.strip()
        if re.match(r"^(?:on\s.+\swrote:|-{2,}\s*original message\s*-{2,}|from:\s)", s, re.I):
            break
        if not s.startswith(">"):
            out.append(line)
    return "\n".join(out)


def suppressed(address: str) -> bool:
    """True when this address asked any company to stop (inbound email saying stop, unsubscribe or remove me)."""
    a = _addr(address)
    if not a:
        return False
    for e in db.q("SELECT from_addr, subject, body FROM emails WHERE direction='in' AND lower(from_addr) LIKE ?",
                  (f"%{a}%",)):
        if _addr(e["from_addr"]) == a and (STOP_WORDS.search(e["subject"] or "")
                                           or STOP_WORDS.search(_own_words(e["body"] or ""))):
            return True
    return False


def placeholder_address(address: str) -> str | None:
    """Why this address is a placeholder or cannot receive mail, or None when it looks real."""
    a = _addr(address) or (address or "").strip().lower()
    if "@" not in a:
        return "it is not an email address"
    local, _, domain = a.rpartition("@")
    if "placeholder" in local or "example" in local or local in _PLACEHOLDER_LOCALS:
        return f"{a} is a placeholder address"
    if domain.endswith(_RESERVED_TLDS) or domain in ("example", "test", "invalid", "localhost"):
        return f"{domain} is a reserved domain that cannot receive email"
    if _PLACEHOLDER_DOMAINS.search(domain):
        return f"{domain} is a placeholder domain"
    return None


def outreach_guard(ctx: Ctx, to: str, subject: str, body: str) -> tuple[str | None, str]:
    """Before send_email queues (design 2.7): (error or None, the body to send, with the footer added)."""
    try:
        company = db.get_company(ctx.company["id"]) or ctx.company
        cs = db.company_settings(company)
        reasons = []
        bad = placeholder_address(to)
        if bad:
            reasons.append(f"{bad}; use a real address you found in a source")
        elif suppressed(to):
            reasons.append(f"{_addr(to)} asked not to be emailed again (they replied stop or unsubscribe)")
        # Someone who wrote to the company first gets a reply, not commercial email: CAN-SPAM exempts these
        # relationship messages, and a support answer with an opt-out footer would read oddly.
        a = _addr(to)
        is_reply = bool(a) and any(_addr(r["from_addr"]) == a for r in db.q(
            "SELECT from_addr FROM emails WHERE company_id=? AND direction='in' AND lower(from_addr) LIKE ?",
            (company["id"], f"%{a}%")))
        address = str(cs.get("postal_address") or "").strip()
        if not address and not is_reply:
            reasons.append("the company has no postal address, which commercial email must carry (CAN-SPAM). Ask the "
                           "owner once to add it in the company's Settings (Postal address), then try again")
        if reasons:
            return "ERROR: email not queued: " + "; ".join(reasons) + ".", body
        if is_reply:
            return None, body
        name = company.get("name") or ""
        flat = " ".join((body or "").lower().split())
        footer = []
        if name.lower() not in flat or " ".join(address.lower().split()) not in flat:
            footer += [name, address]
        if not _OPT_OUT_RE.search(body or ""):
            footer.append(OPT_OUT)
        if footer:
            body = (body or "").rstrip() + "\n\n--\n" + "\n".join(x for x in footer if x)
        return None, body
    except Exception as e:
        _warn("outreach guard", e)
        return None, body


# ---------------------------------------------------------------- deploy

def deploy_guard(ctx: Ctx) -> str | None:
    """Before deploy_site queues: placeholders left in site/ (design 2.6)."""
    try:
        site = workspace.safe_path(ctx.company, "site")
        found = []
        for f in sorted(site.rglob("*")):
            if f.suffix.lower() not in (".html", ".htm") or not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            rel = workspace.rel(ctx.company, f)
            for n, what in checks.find_placeholders(f.read_text(encoding="utf-8", errors="replace"), True):
                found.append(f"{rel} line {n}: {what}")
            if len(found) > 40:
                break
        if found:
            shown = "\n".join(f"- {x}" for x in found[:15])
            if len(found) > 15:
                shown += f"\n- ...and {len(found) - 15} more"
            _refused(ctx, "deploy_site", "placeholders", found[0])
            return ("ERROR: deploy not queued: the site still has placeholders:\n" + shown + "\nReplace each with the "
                    "real value or remove it (an analytics tag or account the company does not have is removed, not "
                    "faked), then call deploy_site again.")
    except Exception as e:
        _warn("deploy guard", e)
    return None


# ---------------------------------------------------------------- action tasks

_VALID_KINDS = ("outreach", "deploy", "post", "purchase", "ask", "research", "build", "write")
_KIND_SYNONYMS = {"email": "outreach", "emails": "outreach", "sales": "outreach", "social": "post",
                  "social_post": "post", "posting": "post", "buy": "purchase", "spend": "purchase",
                  "question": "ask", "code": "build", "engineering": "build", "doc": "write", "document": "write",
                  "writing": "write", "content": "write", "analysis": "research", "release": "deploy",
                  "publish": "deploy"}
# Matched against the start of the title only. The first rules catch titles that start with an action word but
# describe writing ("Email sequence for roofers", "Post ideas"), so they are not taken for the action itself.
_KIND_RULES = [
    ("write", r"(?:e-?mail\s+(?:sequence|templates?|copy|drafts?|campaign\s+plan|plan|strategy|outline|lists?)|"
              r"(?:cold\s+)?outreach\s+(?:plan|strategy|templates?|copy|drafts?|sequence|script|lists?)|"
              r"post\s+(?:ideas|drafts?|calendar|plan|schedule|templates?|copy))\b"),
    ("", r"send\b.*\b(?:to\s+)?(?:the\s+)?owner\b"),
    ("ask", r"ask\s+(?:the\s+)?owner\b"),
    ("outreach", r"(?:send|e-?mail|reach\s+out|contact|cold[- ]e-?mail|follow\s+up\s+with|outreach\s+to)\b"),
    ("deploy", r"(?:(?:re)?deploy\b|go\s+live\b|publish\s+(?:the\s+|our\s+)?(?:site|website|landing\s+page|"
               r"web\s*site|page)\b|launch\s+(?:the\s+|our\s+)?(?:site|website|landing\s+page)\b)"),
    ("post", r"(?:post(?=\s)|tweet\b|publish\s+(?:a\s+|the\s+|our\s+)?(?:post|tweet|thread)s?\b|share\s+on\b)"),
    ("purchase", r"(?:buy|purchase|register\s+(?:the\s+|a\s+|our\s+)?domain)\b"),
    ("research", r"(?:research|find|analy[sz]e|scan|survey|interview|investigate|identify|compare|benchmark|"
                 r"look\s+up)\b"),
    ("build", r"(?:build|implement|create\s+(?:the\s+|a\s+|an\s+)?(?:[\w-]+\s+){0,2}(?:page|site|website|form|app|"
              r"tool)s?\b(?!\s+(?:copy|text|content|doc|document|outline|brief))|fix|code|develop|"
              r"add\s+(?:a\s+|the\s+)?(?:form|page|section))\b"),
    ("write", r"(?:write|draft|edit|rewrite|revise|outline|create\s+(?:a\s+|an\s+|the\s+)?(?:document|doc|brief|"
              r"plan|outline|post|copy|guide|faq|checklist|template|script|one-pager|report|proposal))\b"),
]
_DELIVERABLE_HINTS = [("outreach", r"\be-?mails?\s+(?:queued|sent)\b|\boutreach\s+(?:queued|sent)\b"),
                      ("deploy", r"\b(?:deploy(?:ment)?\s+(?:queued|requested|done)|site\s+(?:deployed|live)|"
                                 r"live\s+site)\b"),
                      ("post", r"\bposts?\s+(?:queued|published|posted)\b"),
                      ("purchase", r"\bpurchase\s+(?:requested|queued|approved)\b")]


def classify_task(title: str, description: str = "", deliverable: str = "", kind: str = "") -> str:
    """outreach | deploy | post | purchase | ask | research | build | write ('' when unknown).

    An explicit kind wins. Otherwise the leading words of the title decide, conservatively: "Write launch post"
    is write and "Find 20 leads" is research. The description is not used because it mentions everything; the
    deliverable only decides when the title does not ("10 emails queued")."""
    k = (kind or "").strip().lower()
    k = _KIND_SYNONYMS.get(k, k)
    if k in _VALID_KINDS:
        return k
    t = (title or "").strip().lower()
    t = re.sub(r"^(?:[\-*#>\s]+|\d+[.)]\s*|\[[^\]]*\]\s*|task\s*:\s*|todo\s*:\s*)+", "", t)
    for name, rx in _KIND_RULES:
        if re.match(rx, t):
            return name
    d = (deliverable or "").lower()
    for name, rx in _DELIVERABLE_HINTS:
        if re.search(rx, d):
            return name
    return ""


def task_guard(ctx: Ctx, kind: str, title: str) -> str | None:
    """Before create_task: refuse action tasks that cannot run today (email not connected). A post task is always
    allowed: without a webhook an approved post still becomes a link the owner posts by hand."""
    try:
        if kind == "outreach":
            cs = db.company_settings(db.get_company(ctx.company["id"]) or ctx.company)
            if not emailer.smtp_configured(cs):
                return ("ERROR: Email is not connected; this task cannot run yet. Ask the owner for a mailbox once, "
                        "or plan work that does not need email.")
    except Exception as e:
        _warn("task guard", e)
    return None


# ---------------------------------------------------------------- fetched pages and verified facts

def _url_key(url: str) -> str:
    """A URL in a comparable form: no scheme, fragment, www. or trailing slash, host lower-cased."""
    u = (url or "").strip().split("#", 1)[0]
    m = re.match(r"^\s*([a-z][a-z0-9+.-]*)://([^/?#]+)(.*)$", u, re.I)
    if not m:
        return u.lower().rstrip("/")
    host = m.group(2).lower()
    host = host[4:] if host.startswith("www.") else host
    path, _, q = (m.group(3) or "").partition("?")
    return host + path.rstrip("/") + (f"?{q}" if q else "")


def _pages_dir(company: dict) -> Path:
    return workspace.root(company) / ".cache" / "pages"


def _page_file(company: dict, url: str) -> Path:
    return _pages_dir(company) / (hashlib.sha256(_url_key(url).encode("utf-8")).hexdigest() + ".txt")


def cache_page(ctx: Ctx, url: str, text: str) -> None:
    """fetch_url returned this text: keep it for record_fact and check_sources. Error pages are not kept, so a 403
    page is never taken for the source. Pages older than 7 days are pruned on the way."""
    try:
        if not text or re.match(r"^HTTP \d{3} fetching ", text):
            return
        d = _pages_dir(ctx.company)
        d.mkdir(parents=True, exist_ok=True)
        f = _page_file(ctx.company, url)
        _write_atomic(f, text)
        os.utime(f)   # an unchanged page fetched again is fresh again
        db.run("INSERT INTO fetched(company_id, url, sha, task_id, run_id, fetched_at) VALUES(?,?,?,?,?,?)",
               (ctx.company["id"], url.strip().split("#", 1)[0], f.stem, (ctx.task or {}).get("id"), ctx.run_id,
                db.now()))
        cutoff = time.time() - PAGE_TTL_DAYS * 86400
        for old in d.glob("*.txt"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception as e:
        _warn("page cache", e)


def _norm(s: str) -> str:
    """Case, spacing and punctuation differences do not make a quote different (design 5.2)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def _fetched_keys(cid: int) -> set[str]:
    return {_url_key(r["url"]) for r in db.q("SELECT url FROM fetched WHERE company_id=? ORDER BY id DESC LIMIT 5000",
                                             (cid,))}


@tool("record_fact", "Save a fact from a page you fetched with fetch_url: the claim, the page's URL and a quote copied "
      "exactly from the page. Holdco checks the quote is on the page. Cite saved facts in files as [fact N].",
      {"claim": S("The fact in your own words, e.g. 'Roofr charges $99 per month'"),
       "url": S("The URL you fetched it from"),
       "quote": S("Words copied exactly from that page that support the claim (12 characters or more)")},
      ("claim", "url", "quote"))
def record_fact(ctx: Ctx, claim: str, url: str, quote: str) -> str:
    claim, url, quote = (claim or "").strip(), (url or "").strip(), (quote or "").strip()
    if not claim:
        return "ERROR: give the claim: the fact in your own words."
    key = _url_key(url)
    if not key or key not in _fetched_keys(ctx.company["id"]):
        return (f"ERROR: fetch the page first: {url or 'that URL'} has not been fetched by this company. Call "
                "fetch_url, then record_fact with a quote copied from the page.")
    nq = _norm(quote)
    if len(nq) < MIN_QUOTE:
        return (f"ERROR: the quote is too short; copy at least {MIN_QUOTE} characters exactly as they appear on the "
                "page.")
    f = _page_file(ctx.company, url)
    if not f.exists():
        return "ERROR: the saved copy of that page has expired (pages are kept 7 days); fetch it again."
    if nq not in _norm(f.read_text(encoding="utf-8", errors="replace")):
        return (f"ERROR: that quote is not on the page you fetched ({url}). Copy a sentence exactly as it appears on "
                "the page.")
    # A claim must not carry a number its quote does not: "Roofr costs $199" backed by a quote saying $99.
    nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", claim)}
    missing = sorted(nums - {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", quote)})
    if missing:
        return (f"ERROR: the claim says {', '.join(missing)} but the quote does not. Quote the part of the page that "
                "contains the number, or fix the claim.")
    fid = db.run("INSERT INTO facts(company_id, agent_id, task_id, claim, url, quote, created_at) VALUES(?,?,?,?,?,?,?)",
                 (ctx.company["id"], (ctx.agent or {}).get("id"), (ctx.task or {}).get("id"), claim[:1000],
                  url.split("#", 1)[0][:1000], quote[:2000], db.now()))
    try:
        bus.emit(ctx.company["id"], "fact", f"recorded fact #{fid}: {_one_line(claim, 160)}",
                 (ctx.agent or {}).get("id"), {"fact_id": fid, "url": url[:500]})
    except Exception:
        pass
    return f"Saved fact #{fid}. Cite it in files as [fact {fid}]."


_STAT_WORDS = re.compile(
    r"\b(?:average|avg|median|mean|market|size|growth|cagr|tam|sam|som|revenue|users|customers|searches|volume|"
    r"per\s+(?:month|year|week|day|lead|job|user|customer|hour)|monthly|annual(?:ly)?|yearly|rate|share|households|"
    r"businesses|companies|population|conversion|ctr|cpc|cpl|cpa|ltv|cac|churn|margin|costs?|prices?|priced|fees?|"
    r"salary|income|spend|budget|worth|valued|valuation|employees|downloads|visitors|traffic|leads)\b", re.I)


def _states_number(line: str) -> bool:
    """Does this line state a number, price, percentage or statistic? Years, dates, times, list numbering, ids and
    step or week numbers do not count."""
    s = _URL_RE.sub(" ", line)
    s = re.sub(r"\[\^?[\w ]+\]", " ", s)                                  # [fact 3], [1], [^2]
    s = re.sub(r"(?<!\d)20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}(?!\d)", " ", s)   # ISO dates
    s = re.sub(r"\b(?:19|20)\d{2}s?\b", " ", s)                            # years
    s = re.sub(r"\b\d{1,2}:\d{2}\b", " ", s)                                # times
    s = re.sub(r"^\s*(?:[-*+]\s+)?\d+[.)]\s+", " ", s)                      # list numbering
    s = re.sub(r"#\d+", " ", s)                                             # request #5, task #3
    s = re.sub(r"\b(?:step|week|day|phase|stage|month|q|h|v|version|tier|level|round|part|section|option|item)\s*\d+"
               r"(?:\.\d+)*\b", " ", s, flags=re.I)
    s = re.sub(r"\b24/7\b|\b1:1\b|\b\d+(?:st|nd|rd|th)\b", " ", s, flags=re.I)
    if re.search(r"[$£€]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars?|euros?|pounds?)\b", s, re.I):
        return True
    if re.search(r"\d\s?%|\b\d[\d,.]*\s?(?:percent|pct)\b", s, re.I):
        return True
    if re.search(r"\b\d[\d,.]*\s?(?:k|m|bn|million|billion|thousand|trillion)\b", s, re.I):
        return True
    if re.search(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b", s):
        return True
    return bool(re.search(r"\b\d+(?:\.\d+)?\b", s) and _STAT_WORDS.search(s))


def source_problems(company: dict, text: str) -> list[tuple[int, str, str]]:
    """(line number, line, reason) for lines that state a number, price, percentage or statistic without a URL this
    company fetched or a [fact N] reference (design 5.3). Lines that say 'estimate' pass, and Markdown reference
    links ([1] with '[1]: https://...' further down) count as the URL they point to."""
    cid = company["id"]
    fetched = _fetched_keys(cid)
    facts = {r["id"] for r in db.q("SELECT id FROM facts WHERE company_id=?", (cid,))}
    lines = (text or "").splitlines()
    refs = {}
    for ln in lines:
        m = re.match(r"^\s*\[\^?([\w-]+)\]:\s*(\S+)", ln)
        if m:
            refs[m.group(1).lower()] = m.group(2)
    out = []
    in_fence = False
    for no, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not s or re.match(r"^\|?[\s:|-]+\|?$", s) or re.match(r"^\[\^?[\w-]+\]:", s):
            continue
        if not _states_number(s) or re.search(r"\bestimat(?:e|ed|es|ion)\b", s, re.I):
            continue
        cited = [int(n) for n in re.findall(r"\[fact\s*#?(\d+)\]", s, re.I)]
        if any(n in facts for n in cited):
            continue
        urls = _URL_RE.findall(s) + [refs[k.lower()] for k in re.findall(r"\[\^?([\w-]+)\]", s) if k.lower() in refs]
        if any(_url_key(u.rstrip(".,;)")) in fetched for u in urls):
            continue
        if cited:
            reason = f"fact {cited[0]} does not exist"
        elif urls:
            reason = f"{_one_line(urls[0], 80)} was never fetched by this company"
        else:
            reason = "no source"
        out.append((no, s, reason))
    return out


@tool("check_sources", "List lines in a document that state a number, price, percentage or statistic without a "
      "source: a URL this company fetched, or a [fact N] reference. Lines labelled 'estimate' pass.",
      {"path": S("A file in the company folder, e.g. research/market.md")}, ("path",))
def check_sources(ctx: Ctx, path: str) -> str:
    p = workspace.safe_path(ctx.company, path)
    if not p.is_file():
        raise ToolError(f"{path} doesn't exist.")
    bad = source_problems(ctx.company, p.read_text(encoding="utf-8", errors="replace"))
    if not bad:
        return "All numbers are sourced."
    rows = "\n".join(f"- Line {n}: {_one_line(t, 140)} ({why})" for n, t, why in bad[:25])
    more = f"\n- ...and {len(bad) - 25} more" if len(bad) > 25 else ""
    return (f"{_plural(len(bad), 'line')} state numbers without a source (a URL this company fetched, or a [fact N] "
            f"reference):\n{rows}{more}\nAdd the source, cite a recorded fact as [fact N], label the line as an "
            "estimate, or remove the number.")


@tool("company_status", "The real state of the company from Holdco's records: website, email, accounts, money, what "
      "waits on the owner, tasks and the latest outbound requests. Check it before saying anything happened.", {})
def company_status(ctx: Ctx) -> str:
    return _cap(status_text(ctx.company), STATUS_CAP)


# ---------------------------------------------------------------- one-time truth check on upgrade

_SCAN_EXT = {".md", ".markdown", ".txt", ".csv", ".json", ".html", ".htm", ".yaml", ".yml", ".log"}
_SCAN_SKIP = {"code", "site", "records", "context"}
_scan_lock = threading.Lock()


def _mark_scanned(cid: int) -> None:
    c = db.get_company(cid)
    if not c:
        return
    try:
        s = json.loads(c.get("settings") or "{}")
    except ValueError:
        s = {}
    s["truth_scan_done"] = True
    db.update("companies", cid, settings=json.dumps(s))


def scan_existing(company: dict) -> list[str]:
    """Run the claims gate once over files written before this version (outside code/, site/ and records/). When
    files claim what the records do not show, the owner hears it once. Files are never changed. Returns the paths
    with problems."""
    cid = company["id"]
    with _scan_lock:
        if cid in _scanned:
            return []
        _scanned.add(cid)
    flagged: list[tuple[str, dict]] = []
    try:
        root = workspace.root(company)
        n = 0
        for f in (sorted(root.rglob("*")) if root.exists() else []):
            rel = f.relative_to(root).as_posix()
            if rel.split("/", 1)[0] in _SCAN_SKIP or any(x.startswith(".") for x in rel.split("/")):
                continue
            if not f.is_file() or f.suffix.lower() not in _SCAN_EXT or f.stat().st_size > 300_000:
                continue
            n += 1
            if n > 400:
                break
            problems = check_claims(company, f.read_text(encoding="utf-8", errors="replace"), rel)
            if problems:
                flagged.append((rel, problems[0]))
        if flagged:
            files = [r for r, _ in flagged]
            shown = ", ".join(files[:10]) + (f" and {len(files) - 10} more" if len(files) > 10 else "")
            bus.emit(cid, "claims", f"Truth check: {_plural(len(files), 'file')} claim things Holdco's records do "
                                    f"not show: {shown}", None,
                     {"where": "truth_scan", "rule": "claims", "files": files[:50], "problem": flagged[0][1]["text"]})
            try:
                from . import notify   # imported here: notify loads tools, which loads this module
                notify.send_later(
                    f"<b>{_html.escape(company.get('name') or '')}</b>: the truth check found "
                    f"{_plural(len(files), 'file')} written before this update that claim things Holdco's records do "
                    f"not show (for example a deploy or emails that never happened): {_html.escape(shown)}. Nothing "
                    "was changed. Your agents now see the real state in their Facts block; you may want to delete "
                    "these files.", kind="important")
            except Exception as e:
                _warn("truth check notice", e)
    except Exception as e:
        _warn("truth check", e)
    finally:
        try:
            _mark_scanned(cid)
        except Exception as e:
            _warn("truth check flag", e)
    return [r for r, _ in flagged]


# ---------------------------------------------------------------- dashboard API

@route("GET", r"/api/companies/(\d+)/facts")
def api_facts(h, m, body):
    """The "What your agents know" card: the same facts block the agents see, plus its parts as data."""
    c = company_or_404(m.group(1))
    st = _state(c)
    return {"text": _facts_text(st), "now": now_text(), "website": dict(st["website"]), "email": dict(st["email"]),
            "social": dict(st["social"]), "money": dict(st["money"]), "waiting": st["waiting"],
            "accounts": st["accounts"], "down": st["down"],
            "signals": {"line": st["signals"]["line"], "replies": st["signals"]["replies"],
                        "signups": st["signals"]["signups"], "revenue": st["signals"]["revenue"]}}
