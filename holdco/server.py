"""HTTP server for the dashboard and its JSON API (standard library only)."""
from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import queue
import re
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import actions, agents, backup, bus, config, context, db, doctor, emailer, llm, notify, planning, playbooks, presets, roles, truth, watchdog, workspace  # noqa: F401
from .routing import ROUTES, HTTPError, company_or_404, route, to_int

_int = to_int
_company_or_404 = company_or_404


def _public_company(c: dict) -> dict:
    cs = db.company_settings(c)
    out = {k: v for k, v in c.items() if k != "settings"}
    secret_keys = ("smtp_password", "imap_password", "netlify_token")
    out["settings"] = {k: ("••••••••" if k in secret_keys and v else v) for k, v in cs.items()}
    return out


def _stats(cid: int) -> dict:
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tasks WHERE company_id=? GROUP BY status", (cid,))}
    return {
        "tasks_open": sum(counts.get(s, 0) for s in ("todo", "in_progress", "blocked", "waiting")),
        "tasks_done": counts.get("done", 0),
        "tasks_blocked": counts.get("blocked", 0) + counts.get("waiting", 0),
        "agents_working": db.scalar("SELECT COUNT(*) FROM agents WHERE company_id=? AND status='working'", (cid,)) or 0,
        "agents_total": db.scalar("SELECT COUNT(*) FROM agents WHERE company_id=?", (cid,)) or 0,
        "approvals": db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pending'", (cid,)) or 0,
        "last_event": db.scalar("SELECT created_at FROM events WHERE company_id=? ORDER BY id DESC LIMIT 1", (cid,)),
        "done_today": db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status='done' AND updated_at>=?",
                                (cid, db.now()[:10])) or 0,
    }


# ======================= overview & settings =======================

@route("GET", "/api/state")
def api_state(h, m, body):
    companies = []
    for c in db.q("SELECT * FROM companies ORDER BY status='archived', id"):
        companies.append({**_public_company(c), "stats": _stats(c["id"])})
    s = db.get_settings()
    return {
        "companies": companies,
        "approvals": db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'") or 0,
        "running": agents.running_count(False),
        "scheduler_enabled": s.get("scheduler_enabled", True),
        "kill_switch": bool(s.get("kill_switch")),
        "doctor": doctor.summary(),
        "model": s.get("default_model"),
        "agents": db.q("SELECT id, company_id, name, role, preset, title, status, enabled FROM agents ORDER BY id"),
        "version": __import__("holdco").__version__,
    }


@route("GET", "/api/settings")
def api_settings(h, m, body):
    s = db.get_settings()
    return {"settings": s, "policy_actions": config.POLICY_ACTIONS, "locked": sorted(config.LOCKED_POLICIES),
            "data_dir": str(config.DATA_DIR)}


@route("PUT", "/api/settings")
def api_settings_put(h, m, body):
    try:
        s = db.set_settings(body or {})
    except ValueError as e:   # a malformed chat id or an unknown notify level: tell the owner, save nothing
        raise HTTPError(400, str(e))
    bus.notify("settings")
    return {"settings": s}


@route("GET", "/api/llm/models")
def api_models(h, m, body):
    try:
        return {"ok": True, "models": llm.list_models()}
    except llm.LLMError as e:
        return {"ok": False, "error": str(e), "models": []}


@route("POST", "/api/llm/test")
def api_llm_test(h, m, body):
    s = db.get_settings()
    ok, message = llm.tool_ping((body or {}).get("model") or s["default_model"], s)
    return {"ok": ok, "message": message}


@route("GET", "/api/presets")
def api_presets(h, m, body):
    templates = {}
    for k, t in presets.TEMPLATES.items():
        templates[k] = {**t, "team": [{"key": mk, "name": presets.PRESETS[mk]["name"],
                                        "title": presets.PRESETS[mk]["title"]} for mk in t["members"]]}
    return {"presets": presets.summary_list(), "categories": presets.CATEGORIES, "templates": templates,
            "default_template": presets.DEFAULT_TEMPLATE}


# ======================= ideas & companies =======================

@route("POST", "/api/ideas")
def api_ideas(h, m, body):
    b = body or {}
    try:
        ideas = agents.generate_ideas(b.get("interests", ""), b.get("skills", ""), b.get("budget", ""), b.get("hours", ""))
    except llm.LLMError as e:
        raise HTTPError(502, str(e))
    return {"ideas": ideas}


@route("POST", "/api/companies")
def api_company_create(h, m, body):
    b = body or {}
    idea = (b.get("idea") or "").strip()
    if len(idea) < 8:
        raise HTTPError(400, "Describe the idea in at least a sentence.")
    if not b.get("force"):
        # Phase 0 preflight: refuse to found on a broken dependency and say why (the page offers "Found anyway").
        try:
            pf = doctor.preflight()
        except NotImplementedError:
            pf = {"ok": True, "checks": [], "soft": []}
        if not pf["ok"]:
            bad = [r for r in pf["checks"] if r["status"] == "bad"]
            raise PreflightError("Fix this before founding: " + "; ".join(r["label"] + ": " + r["detail"] for r in bad),
                                 pf["checks"])
    c = agents.create_company(idea, b.get("name", ""), b.get("context", ""), b.get("template", ""))
    return {"company": _public_company(c)}


class PreflightError(HTTPError):
    def __init__(self, message: str, checks: list[dict]):
        super().__init__(409, message)
        self.checks = checks


@route("POST", "/api/kill")
def api_kill(h, m, body):
    # One helper for the dashboard button and the Telegram /stop and /resume commands: it saves the setting,
    # emits the event and confirms on the phone without the identical-text rule, so a second ON within the
    # hour is confirmed too (and the send runs in the background, so this answers at once).
    on = notify.set_kill_switch(bool((body or {}).get("on", True)), by="owner")
    return {"kill_switch": on}


@route("GET", "/api/audit")
def api_audit(h, m, body):
    cid = _int(h.query.get("company_id", [0])[0])
    limit = max(1, min(_int(h.query.get("limit", [100])[0], 100), 500))
    sql = "SELECT a.*, c.name AS company_name FROM audit a LEFT JOIN companies c ON c.id=a.company_id"
    params: list = []
    if cid:
        sql += " WHERE a.company_id=?"
        params.append(cid)
    rows = db.q(sql + f" ORDER BY a.id DESC LIMIT {limit}", params)
    for r in rows:
        r["payload"] = db.loads(r["payload"], {})
    return {"audit": rows}


@route("GET", r"/api/companies/(\d+)")
def api_company(h, m, body):
    c = _company_or_404(m.group(1))
    team = db.q("SELECT a.*, t.title AS task_title FROM agents a LEFT JOIN tasks t ON t.id=a.current_task_id "
                "WHERE a.company_id=? ORDER BY a.id", (c["id"],))
    metrics = db.q("SELECT name, value, note, created_at FROM metrics WHERE id IN (SELECT MAX(id) FROM metrics "
                   "WHERE company_id=? GROUP BY name) ORDER BY name", (c["id"],))
    cs = db.company_settings(c)
    return {"company": _public_company(c), "agents": team, "stats": _stats(c["id"]), "metrics": metrics,
            "email_ready": emailer.smtp_configured(cs), "inbox_ready": emailer.imap_configured(cs),
            "has_site": workspace.safe_path(c, "site/index.html").exists(),
            "folder": str(workspace.root(c))}


# Company settings Holdco manages itself (staged founding, notices, the one-time truth check). The dashboard never
# sends them; refusing them here keeps any other client from moving a founding stage by hand.
INTERNAL_COMPANY_SETTINGS = {"founding_stage", "founding_retry", "founding_research_note", "founding_context",
                             "last_waiting_notice", "budget_notice", "truth_scan_done", "name_locked", "template"}


@route("PATCH", r"/api/companies/(\d+)")
def api_company_patch(h, m, body):
    c = _company_or_404(m.group(1))
    b = body or {}
    fields = {}
    caps_before = planning.capability_hash(db.company_settings(c))
    if "status" in b and b["status"] in ("active", "paused", "archived"):
        fields["status"] = b["status"]
        bus.emit(c["id"], "info", {"active": "resumed the company", "paused": "paused the company",
                                   "archived": "archived the company"}[b["status"]], None, {"by": "owner"})
    for k in ("name", "mission", "stage", "charter"):
        if k in b and isinstance(b[k], str):
            fields[k] = b[k]
    if "charter" in fields:
        p = workspace.safe_path(c, "docs/charter.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fields["charter"], encoding="utf-8")
    if "settings" in b and isinstance(b["settings"], dict):
        cs = db.company_settings(c)
        for k, v in b["settings"].items():
            if k not in config.DEFAULT_COMPANY_SETTINGS or k in INTERNAL_COMPANY_SETTINGS:
                continue
            if isinstance(v, str) and v == "••••••••":
                continue  # unchanged secret
            if k in ("cycle_minutes", "daily_email_limit", "smtp_port", "imap_port"):
                v = max(1, _int(v, config.DEFAULT_COMPANY_SETTINGS[k]))
            if k == "daily_token_budget":
                v = max(0, _int(v, config.DEFAULT_COMPANY_SETTINGS[k]))   # 0 = no limit
            if k == "pinned_context":
                v = [str(x)[:300] for x in v][:40] if isinstance(v, list) else []
            if k == "policies" and isinstance(v, dict):
                v = {a: p for a, p in v.items() if a in config.POLICY_ACTIONS and p in ("approve", "auto", "off", "")
                     and a not in config.LOCKED_POLICIES}
                v = {a: p for a, p in v.items() if p}
            cs[k] = v
        fields["settings"] = json.dumps(cs)
        # Phase 0.5: a new mailbox, token, site address, postal address, standing orders or business type
        # changes what the team can do, so the CEO plans with it at the next tick.
        if planning.capability_hash(cs) != caps_before:
            fields["cycle_requested"] = 1
    if "charter" in fields and (fields["charter"] or "") != (c.get("charter") or ""):
        fields["cycle_requested"] = 1   # the owner rewrote the charter: plan against the new one
    if fields:
        db.update("companies", c["id"], **fields)
    bus.notify("company", company_id=c["id"])
    return {"company": _public_company(db.get_company(c["id"]))}


@route("DELETE", r"/api/companies/(\d+)")
def api_company_delete(h, m, body):
    c = _company_or_404(m.group(1))
    if (body or {}).get("confirm") != c["name"]:
        raise HTTPError(400, "Type the company name to confirm.")
    db.update("companies", c["id"], status="archived")
    for t in ("agents", "tasks", "memory", "approvals", "events", "messages", "runs", "emails", "metrics"):
        db.run(f"DELETE FROM {t} WHERE company_id=?", (c["id"],))
    db.run("DELETE FROM companies WHERE id=?", (c["id"],))
    workspace.remove_company_folder(c)
    bus.notify("companies")
    return {"ok": True}


@route("POST", r"/api/companies/(\d+)/cycle")
def api_cycle(h, m, body):
    c = _company_or_404(m.group(1))
    ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
    if not ceo:
        raise HTTPError(400, "This company has no CEO agent.")
    if agents.is_running(ceo["id"]):
        db.update("companies", c["id"], cycle_requested=1)   # the scheduler plans as soon as the CEO is free
        return {"ok": False, "message": f"{ceo['name']} is busy; they'll plan as soon as they're free."}
    # The owner asked: the baseline is recorded and the cycle's header never says "Nothing has changed".
    planning.cycle_started(c, requested=True)
    if not agents.start(ceo["id"], "cycle", requested=True):
        db.update("companies", c["id"], cycle_requested=1)
        return {"ok": False, "message": f"{ceo['name']} is busy; they'll plan as soon as they're free."}
    return {"ok": True, "message": f"{ceo['name']} is planning now."}


@route("POST", r"/api/companies/(\d+)/refound")
def api_refound(h, m, body):
    c = _company_or_404(m.group(1))
    ok = agents.found(c["id"])
    return {"ok": ok}


# ======================= agents =======================

@route("POST", r"/api/companies/(\d+)/agents")
def api_agent_create(h, m, body):
    c = _company_or_404(m.group(1))
    b = body or {}
    try:
        aid = roles.add_agent(c["id"], preset=(b.get("preset") or "").strip(), role=(b.get("role") or "").strip(),
                              name=(b.get("name") or "").strip(), title=(b.get("title") or "").strip(),
                              focus=b.get("focus", ""), model=b.get("model", ""))
    except roles.TeamError as e:
        raise HTTPError(400, str(e))
    a = db.get_agent(aid)
    bus.emit(c["id"], "team", f"added {a['name']} ({a['title']}) to the team", None, {"by": "owner"})
    bus.notify("agents", company_id=c["id"])
    return {"agent": a}


@route("PATCH", r"/api/agents/(\d+)")
def api_agent_patch(h, m, body):
    a = db.get_agent(_int(m.group(1)))
    if not a:
        raise HTTPError(404, "Agent not found")
    b = body or {}
    fields = {}
    for k in ("name", "title", "focus", "model"):
        if k in b and isinstance(b[k], str):
            fields[k] = b[k]
    if "enabled" in b:
        fields["enabled"] = 1 if b["enabled"] else 0
    if "routines_on" in b:
        fields["routines_on"] = 1 if b["routines_on"] else 0
    if b.get("reasoning") in ("", "low", "medium", "high"):
        fields["reasoning"] = b["reasoning"]
    db.update("agents", a["id"], **fields)
    bus.notify("agents", company_id=a["company_id"])
    return {"agent": db.get_agent(a["id"])}


@route("DELETE", r"/api/agents/(\d+)")
def api_agent_delete(h, m, body):
    a = db.get_agent(_int(m.group(1)))
    if not a:
        raise HTTPError(404, "Agent not found")
    if a["role"] == "ceo":
        raise HTTPError(400, "Every company needs its CEO. Disable it instead.")
    db.run("DELETE FROM agents WHERE id=?", (a["id"],))
    db.run("UPDATE tasks SET agent_id=NULL, role='ceo' WHERE agent_id=? AND status IN ('todo','blocked','waiting')", (a["id"],))
    bus.notify("agents", company_id=a["company_id"])
    return {"ok": True}


@route("POST", r"/api/agents/(\d+)/run")
def api_agent_run(h, m, body):
    a = db.get_agent(_int(m.group(1)))
    if not a:
        raise HTTPError(404, "Agent not found")
    if a["role"] == "ceo" and not (body or {}).get("task_id"):
        ok = agents.start(a["id"], "cycle", requested=True)   # the owner started it from the Team tab
    else:
        t = db.one("SELECT * FROM tasks WHERE company_id=? AND agent_id=? AND status='todo' ORDER BY priority, id LIMIT 1",
                   (a["company_id"], a["id"]))
        if not t:
            return {"ok": False, "message": f"{a['name']} has no tasks waiting. Add one on the Tasks tab."}
        ok = agents.start(a["id"], "task", task_id=t["id"])
    return {"ok": ok, "message": "Started." if ok else f"{a['name']} is already working."}


@route("GET", r"/api/companies/(\d+)/runs")
def api_runs(h, m, body):
    c = _company_or_404(m.group(1))
    qs = h.query
    agent_id = _int(qs.get("agent_id", [0])[0])
    sql = ("SELECT r.id, r.agent_id, r.task_id, r.kind, r.status, r.summary, r.tokens, r.steps, r.started_at, "
           "r.finished_at, a.name AS agent_name FROM runs r LEFT JOIN agents a ON a.id=r.agent_id WHERE r.company_id=?")
    params: list = [c["id"]]
    if agent_id:
        sql += " AND r.agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY r.id DESC LIMIT 40"
    return {"runs": db.q(sql, params)}


@route("GET", r"/api/runs/(\d+)")
def api_run(h, m, body):
    r = db.one("SELECT r.*, a.name AS agent_name FROM runs r LEFT JOIN agents a ON a.id=r.agent_id WHERE r.id=?",
               (_int(m.group(1)),))
    if not r:
        raise HTTPError(404, "Run not found")
    r["transcript"] = db.loads(r["transcript"], [])
    return {"run": r}


# ======================= tasks =======================

@route("GET", r"/api/companies/(\d+)/tasks")
def api_tasks(h, m, body):
    c = _company_or_404(m.group(1))
    base = "SELECT t.*, a.name AS agent_name FROM tasks t LEFT JOIN agents a ON a.id=t.agent_id WHERE t.company_id=? "
    open_rows = db.q(base + "AND t.status NOT IN ('done','cancelled') ORDER BY t.priority, t.id", (c["id"],))
    closed = db.q(base + "AND t.status IN ('done','cancelled') ORDER BY t.updated_at DESC LIMIT 40", (c["id"],))
    return {"tasks": open_rows + closed}


@route("POST", r"/api/companies/(\d+)/tasks")
def api_task_create(h, m, body):
    c = _company_or_404(m.group(1))
    b = body or {}
    title = (b.get("title") or "").strip()
    if not title:
        raise HTTPError(400, "Give the task a title.")
    agent = db.get_agent(_int(b.get("agent_id"))) if b.get("agent_id") else None
    if not agent or agent["company_id"] != c["id"]:
        agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
    ts = db.now()
    kind = truth.classify_task(title, b.get("description", ""), b.get("deliverable") or "", b.get("kind") or "")
    tid = db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, created_by,"
                 " deliverable, acceptance, kind, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (c["id"], title[:200], b.get("description", ""), agent["role"], agent["id"], "todo",
                  max(1, min(_int(b.get("priority"), 1), 3)), "owner", (b.get("deliverable") or "").strip()[:200],
                  (b.get("acceptance") or "").strip()[:2000], kind, ts, ts))
    bus.emit(c["id"], "task", f"created task #{tid} for {agent['name']}: {title}", None, {"task_id": tid, "by": "owner"})
    return {"task": db.get_task(tid)}


@route("PATCH", r"/api/tasks/(\d+)")
def api_task_patch(h, m, body):
    t = db.get_task(_int(m.group(1)))
    if not t:
        raise HTTPError(404, "Task not found")
    b = body or {}
    fields = {"updated_at": db.now()}
    if b.get("status") in ("todo", "done", "blocked", "waiting", "cancelled"):
        fields["status"] = b["status"]
    for k in ("title", "description", "deliverable", "acceptance"):
        if isinstance(b.get(k), str):
            fields[k] = b[k]
    if "priority" in b:
        fields["priority"] = max(1, min(_int(b["priority"], 2), 3))
    if b.get("agent_id"):
        a = db.get_agent(_int(b["agent_id"]))
        if a and a["company_id"] == t["company_id"]:
            fields.update(agent_id=a["id"], role=a["role"])
    if isinstance(b.get("note"), str) and b["note"].strip():
        fields["result"] = ((t["result"] or "") + f"\n[Owner] {b['note'].strip()}").strip()
    db.update("tasks", t["id"], **fields)
    bus.notify("tasks", company_id=t["company_id"])
    return {"task": db.get_task(t["id"])}


# ======================= chat, memory, metrics, email, events =======================

@route("GET", r"/api/companies/(\d+)/messages")
def api_messages(h, m, body):
    c = _company_or_404(m.group(1))
    rows = db.q("SELECT m.*, a.name AS agent_name, a.role AS agent_role FROM messages m LEFT JOIN agents a "
                "ON a.id=m.agent_id WHERE m.company_id=? ORDER BY m.id DESC LIMIT 120", (c["id"],))
    return {"messages": list(reversed(rows)),
            "busy": [a["id"] for a in db.q("SELECT id FROM agents WHERE company_id=?", (c["id"],))
                     if agents.is_running(a["id"])]}


@route("POST", r"/api/companies/(\d+)/messages")
def api_message_send(h, m, body):
    c = _company_or_404(m.group(1))
    text = ((body or {}).get("content") or "").strip()
    if not text:
        raise HTTPError(400, "Write a message first.")
    return agents.send_chat(c["id"], text, _int((body or {}).get("agent_id")) or None)


@route("GET", r"/api/companies/(\d+)/memory")
def api_memory(h, m, body):
    c = _company_or_404(m.group(1))
    return {"memory": db.q("SELECT m.*, a.name AS agent_name FROM memory m LEFT JOIN agents a ON a.id=m.agent_id "
                           "WHERE m.company_id=? ORDER BY m.pinned DESC, m.id DESC LIMIT 300", (c["id"],))}


@route("POST", r"/api/companies/(\d+)/memory")
def api_memory_add(h, m, body):
    c = _company_or_404(m.group(1))
    text = ((body or {}).get("content") or "").strip()
    if not text:
        raise HTTPError(400, "Write the note first.")
    mid = db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
                 (c["id"], None, "owner", text, 1 if (body or {}).get("pinned") else 0, db.now()))
    return {"id": mid}


@route("PATCH", r"/api/memory/(\d+)")
def api_memory_patch(h, m, body):
    if "pinned" in (body or {}):
        db.run("UPDATE memory SET pinned=? WHERE id=?", (1 if body["pinned"] else 0, _int(m.group(1))))
    return {"ok": True}


@route("DELETE", r"/api/memory/(\d+)")
def api_memory_delete(h, m, body):
    db.run("DELETE FROM memory WHERE id=?", (_int(m.group(1)),))
    return {"ok": True}


@route("GET", r"/api/companies/(\d+)/metrics")
def api_metrics(h, m, body):
    c = _company_or_404(m.group(1))
    return {"metrics": db.q("SELECT * FROM metrics WHERE company_id=? ORDER BY id DESC LIMIT 500", (c["id"],))}


@route("GET", r"/api/companies/(\d+)/emails")
def api_emails(h, m, body):
    c = _company_or_404(m.group(1))
    return {"emails": db.q("SELECT * FROM emails WHERE company_id=? ORDER BY id DESC LIMIT 100", (c["id"],))}


@route("POST", r"/api/companies/(\d+)/emails/check")
def api_emails_check(h, m, body):
    c = _company_or_404(m.group(1))
    if not emailer.imap_configured(db.company_settings(c)):
        raise HTTPError(400, "Add IMAP details in this company's Settings first.")
    n = emailer.poll_inbox(c)
    return {"new": n}


@route("POST", r"/api/companies/(\d+)/emails/test")
def api_emails_test(h, m, body):
    c = _company_or_404(m.group(1))
    to = ((body or {}).get("to") or "").strip()
    try:
        return {"ok": True, "message": emailer.send(c, to, f"Test from {c['name']}",
                                                    "This is a test email from Holdco. Email sending works.")}
    except emailer.EmailError as e:
        return {"ok": False, "message": str(e)}


@route("GET", "/api/events")
def api_events(h, m, body):
    qs = h.query
    cid = _int(qs.get("company_id", [0])[0])
    before = _int(qs.get("before", [0])[0])
    limit = max(1, min(_int(qs.get("limit", [80])[0], 80), 300))
    sql = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if cid:
        sql += " AND company_id=?"
        params.append(cid)
    agent = _int(qs.get("agent_id", [0])[0])
    if agent:
        sql += " AND agent_id=?"
        params.append(agent)
    for key, op in (("kinds", "IN"), ("exclude", "NOT IN")):
        names = [k for k in (qs.get(key, [""])[0] or "").split(",") if re.fullmatch(r"[a-z_]{1,24}", k)]
        if names:
            sql += f" AND kind {op} ({','.join('?' * len(names))})"
            params.extend(names)
    if before:
        sql += " AND id<?"
        params.append(before)
    sql += f" ORDER BY id DESC LIMIT {limit}"
    rows = db.q(sql, params)
    for r in rows:
        r["data"] = db.loads(r["data"], {})
    return {"events": rows}


# ======================= approvals =======================

@route("GET", "/api/approvals")
def api_approvals(h, m, body):
    status = h.query.get("status", ["pending"])[0]
    cid = _int(h.query.get("company_id", [0])[0])
    sql = ("SELECT p.*, a.name AS agent_name, a.role AS agent_role, c.name AS company_name FROM approvals p "
           "LEFT JOIN agents a ON a.id=p.agent_id LEFT JOIN companies c ON c.id=p.company_id WHERE ")
    params: list = []
    if status == "pending":
        sql += "p.status='pending'"
    else:
        sql += "p.status!='pending'"
    if cid:
        sql += " AND p.company_id=?"
        params.append(cid)
    sql += " ORDER BY p.id DESC LIMIT 100"
    rows = db.q(sql, params)
    for r in rows:
        r["payload"] = db.loads(r["payload"], {})
        # Phase 1: the risk tag on every card (a connector call carries its own)
        r["tag"] = r["payload"].get("tag") if r["action"] == "connector_call" else actions.TAG_OF_ACTION.get(r["action"], "")
    return {"approvals": rows, "labels": actions.ACTION_LABELS}


@route("POST", r"/api/approvals/(\d+)")
def api_approval_decide(h, m, body):
    b = body or {}
    decision = b.get("decision")
    if decision not in ("approve", "reject", "answer"):
        raise HTTPError(400, "decision must be approve, reject or answer")
    try:
        row = actions.decide(_int(m.group(1)), "approve" if decision == "answer" else decision,
                             b.get("payload") or None, b.get("feedback", ""), b.get("answer", ""))
    except ValueError as e:
        raise HTTPError(400, str(e))
    row["payload"] = db.loads(row["payload"], {})
    return {"approval": row}


@route("POST", r"/api/approvals/(\d+)/retry")
def api_approval_retry(h, m, body):
    try:
        row = actions.retry(_int(m.group(1)))
    except ValueError as e:
        raise HTTPError(400, str(e))
    return {"approval": row}


# ======================= files =======================

@route("GET", r"/api/companies/(\d+)/files")
def api_files(h, m, body):
    c = _company_or_404(m.group(1))
    return {"files": workspace.tree(c, ".", depth=6, limit=800)}


@route("GET", r"/api/companies/(\d+)/file")
def api_file(h, m, body):
    c = _company_or_404(m.group(1))
    path = h.query.get("path", [""])[0]
    try:
        p = workspace.safe_path(c, path)
    except workspace.PathError as e:
        raise HTTPError(400, str(e))
    if not p.is_file():
        raise HTTPError(404, "File not found")
    raw = p.read_bytes()[:2_000_000]
    if b"\x00" in raw[:4000]:
        return {"path": path, "binary": True, "size": p.stat().st_size}
    return {"path": path, "binary": False, "content": raw.decode("utf-8", "replace"), "size": p.stat().st_size}


@route("PUT", r"/api/companies/(\d+)/file")
def api_file_put(h, m, body):
    c = _company_or_404(m.group(1))
    b = body or {}
    try:
        p = workspace.safe_path(c, b.get("path", ""))
    except workspace.PathError as e:
        raise HTTPError(400, str(e))
    if p == workspace.root(c).resolve() or p.is_dir():
        raise HTTPError(400, "Pick a file path.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(b.get("content", ""), encoding="utf-8", newline="")
    bus.emit(c["id"], "file", f"edited {workspace.rel(c, p)}", None, {"path": workspace.rel(c, p), "by": "owner"})
    return {"ok": True}


# ======================= handler =======================

class Handler(BaseHTTPRequestHandler):
    server_version = "Holdco"
    query: dict = {}

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # --- helpers
    def _send(self, status: int, payload, ctype="application/json; charset=utf-8", extra: dict | None = None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _auth_ok(self) -> bool:
        if not config.PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, pw = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
                return hmac.compare_digest(pw, config.PASSWORD)
            except (ValueError, UnicodeDecodeError):
                return False
        return False

    def _handle(self, method: str):
        if not self._auth_ok():
            self._send(401, {"error": "Password required"}, extra={"WWW-Authenticate": 'Basic realm="Holdco"'})
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self.query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/stream" and method == "GET":
                return self._stream()
            if path.startswith("/api/"):
                if method != "GET" and self.headers.get("X-Holdco") != "1":
                    raise HTTPError(403, "Missing X-Holdco header")
                body = None
                length = _int(self.headers.get("Content-Length"), 0)
                if length:
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        raise HTTPError(415, "Send JSON")
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except ValueError:
                        raise HTTPError(400, "Invalid JSON")
                for mth, rx, fn in ROUTES:
                    if mth == method and (mm := rx.match(path)):
                        return self._send(200, fn(self, mm, body))
                raise HTTPError(404, "Not found")
            if method not in ("GET", "HEAD"):
                raise HTTPError(405, "Method not allowed")
            if path == "/oauth/callback" and method == "GET":   # Phase 1: the OAuth sign-in comes back here
                from . import oauth
                code, html = oauth.callback_page({k: v[0] for k, v in self.query.items()})
                return self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/preview/") or path.startswith("/files/"):
                return self._company_file(path)
            return self._static(path)
        except PreflightError as e:
            self._send(e.status, {"error": e.message, "checks": e.checks})
        except HTTPError as e:
            self._send(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                # An unexpected error may quote a URL; the bot token must not travel to the browser in it.
                self._send(500, {"error": notify.scrub(f"{type(e).__name__}: {e}")})
            except OSError:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    # --- static & files
    def _file(self, p: Path, headers: dict | None = None):
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype, headers)

    def _static(self, path: str):
        if path in ("/", "/index.html"):
            return self._file(config.STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            p = (config.STATIC_DIR / path[len("/static/"):]).resolve()
            if config.STATIC_DIR.resolve() in p.parents and p.is_file():
                return self._file(p)
        raise HTTPError(404, "Not found")

    def _company_file(self, path: str):
        prefix, _, rest = path.lstrip("/").partition("/")
        slug, _, sub = rest.partition("/")
        c = db.one("SELECT * FROM companies WHERE slug=?", (urllib.parse.unquote(slug),))
        if not c:
            raise HTTPError(404, "Company not found")
        sub = urllib.parse.unquote(sub)
        if prefix == "preview":
            sub = "site/" + sub
            if sub.endswith("/") or sub == "site/":
                sub += "index.html"
        try:
            p = workspace.safe_path(c, sub)
        except workspace.PathError:
            raise HTTPError(400, "Bad path")
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file():
            raise HTTPError(404, "Not found")
        # Agent-written pages are sandboxed so they can't call the dashboard API.
        self._file(p, {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups"})

    # --- live events
    def _stream(self):
        qu = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = qu.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(qu)



class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows this would let two copies share a port


def make_server(host: str, port: int) -> Server:
    return Server((host, port), Handler)
