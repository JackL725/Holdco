"""Outbound actions and the approval inbox.

Agents never perform these directly. They call `gate()`, which, depending on the company's policy,
runs the action now, queues it for the owner's approval, or refuses it.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import bus, db, emailer, web, workspace

ACTION_LABELS = {
    "send_email": "Send email",
    "social_post": "Social post",
    "deploy_site": "Deploy website",
    "http_post": "Outside request",
    "run_code": "Run code",
    "spend_money": "Purchase",
    "question": "Question",
    "connector_call": "Connected service call",
    "decision": "Decision",
}


def _summary(action: str, p: dict) -> str:
    if action == "decision":
        return (p.get("question") or "")[:140]
    if action == "connector_call":
        args = json.dumps(p.get("args") or {}, ensure_ascii=False, default=str)
        return f"{p.get('connector')} {p.get('tool')}({args[:90]}) [{p.get('tag')}]"
    if action == "send_email":
        return f"Email to {p.get('to')}: {p.get('subject')}"
    if action == "social_post":
        return f"{(p.get('platform') or 'social').title()} post: {(p.get('content') or '')[:90]}"
    if action == "deploy_site":
        return "Publish the website from site/"
    if action == "http_post":
        return f"{p.get('method', 'POST')} {p.get('url')}"
    if action == "run_code":
        first = (p.get("code") or "").strip().splitlines()[:1]
        return f"Run {p.get('language', 'python')}: {first[0][:90] if first else ''}"
    if action == "spend_money":
        return f"Buy {p.get('item')} for ${p.get('amount_usd')} ({p.get('vendor', '')})"
    if action == "question":
        return (p.get("question") or "")[:140]
    return action


# Phase 1: the risk tag of each built-in outbound action, shown on the Inbox card.
TAG_OF_ACTION = {"send_email": "contact", "social_post": "publish", "deploy_site": "publish", "http_post": "publish",
                 "run_code": "write", "spend_money": "spend", "question": "read", "decision": "read"}


def gate(company: dict, agent: dict, task_id: int | None, action: str, payload: dict,
         policy_action: str | None = None, tainted: list[str] | None = None) -> str:
    # Phase 2: a decision is queued like a question (always asks, deduplicated), never run by a policy.
    """Called by agent tools for any outbound action. policy_action (Phase 1) names the policy to apply when it
    differs from the action, as for connector_call, whose policy comes from the tool's risk tag."""
    if action in ("question", "decision"):
        policy = "approve"
    else:
        policy = db.effective_policy(company, policy_action or action)
    if policy == "off":
        return f"Not allowed: the owner has turned off '{ACTION_LABELS.get(action, action)}' for this company."
    if action not in ("question", "decision"):
        # Phase 3: a session that read outside content asks, and the trust ladder sits in front of the policy.
        from . import taint, trust   # imported here: both import modules that import this one
        sources = taint.current_sources() if tainted is None else list(tainted)
        policy, why = trust.gate_policy(company, policy_action or action, payload, policy, sources)
        if why and policy == "approve":
            payload = {**payload, "_why_asked": why}
    killed = False
    if policy == "auto" and db.get_settings().get("kill_switch"):
        policy, killed = "approve", True   # the kill switch freezes outbound actions: queue instead of running
    if policy == "auto":
        try:
            result = execute(company, action, payload)
        except Exception as e:  # report failures back to the agent
            bus.emit(company["id"], "error", f"{ACTION_LABELS.get(action, action)} failed: {e}", agent["id"])
            audit(company["id"], action, agent["name"], "policy:auto", payload, f"FAILED: {e}")
            return f"FAILED: {e}"
        bus.emit(company["id"], "action", f"{ACTION_LABELS.get(action, action)}: {_summary(action, payload)}",
                 agent["id"], {"action": action})
        audit(company["id"], action, agent["name"], "policy:auto", payload, result)
        return result

    # approve: queue it (deduplicated)
    blob = json.dumps(payload, sort_keys=True)
    for row in db.q("SELECT id, payload FROM approvals WHERE company_id=? AND action=? AND status='pending'",
                    (company["id"], action)):
        old = db.loads(row["payload"], {}) or {}
        if action == "decision" and _same_question(old.get("question"), payload.get("question")):
            # A decision carries its own deadline, so the same question asked again rarely has an identical payload.
            return (f"Already waiting for the owner as decision #{row['id']}. Don't resubmit; continue with other "
                    f"work, or finish with status 'waiting'.")
        if json.dumps(old, sort_keys=True) == blob:
            return f"Already waiting for the owner as request #{row['id']}. Don't resubmit; continue with other work."
    summary = _summary(action, payload)
    aid = db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (company["id"], agent["id"], task_id, action, summary, blob, "pending", db.now()))
    verb = {"question": "asked the owner", "decision": "asked the owner to decide"}.get(action, "requested approval")
    bus.emit(company["id"], "approval", f"{verb}: {summary}", agent["id"], {"approval_id": aid, "action": action})
    bus.notify("approvals")
    _records_changed(company["id"])
    from . import notify  # imported here: notify and watchdog import tools, which imports this module
    try:
        notify.approval_requested(db.one("SELECT * FROM approvals WHERE id=?", (aid,)))
    except NotImplementedError:
        pass
    except Exception as e:  # a phone that cannot be reached never blocks an agent
        print(f"notify failed: {e}")
    if action == "question":
        return (f"Question #{aid} sent to the owner. Their answer will appear in your notes. If you can't continue "
                f"without it, finish with status 'waiting'.")
    if action == "decision":
        if not payload.get("reversible", True):
            return (f"Decision #{aid} sent to the owner. It cannot be undone, so it waits for the owner's answer. "
                    f"Continue with work that does not depend on it, or finish with status 'waiting'.")
        deadline = str(payload.get("deadline_at") or "").replace("T", " ")[:16]
        return (f"Decision #{aid} sent to the owner. If it is reversible and unanswered by {deadline} UTC, "
                f"'{payload.get('recommended', '')}' will be taken. Continue with work that does not depend on it, "
                f"or finish with status 'waiting'.")
    if killed:
        # The agent should know this was not the normal policy: nothing outbound runs until the owner resumes.
        return (f"Kill switch is on: queued for the owner as request #{aid}. Don't resubmit it. Continue with "
                f"other work; the decision will appear in your notes.")
    return (f"Queued for the owner's approval as request #{aid}. Don't resubmit it. Continue with other work; "
            f"the decision will appear in your notes.")


def decide(approval_id: int, decision: str, payload: dict | None = None, feedback: str = "",
           answer: str = "", decided_by: str = "owner") -> dict:
    """The owner approves or rejects a request (or answers a question or a decision). decided_by (Phase 2):
    owner, telegram or deadline."""
    row = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not row:
        raise ValueError("Request not found")
    if row["status"] != "pending":
        raise ValueError(f"Request #{approval_id} is already {row['status']}")
    if decision == "approve" and row["action"] not in ("question", "decision") and db.get_settings().get("kill_switch"):
        raise ValueError("The kill switch is on. Turn it off to run approvals.")
    company = db.get_company(row["company_id"])
    p = db.loads(row["payload"], {})
    if payload:
        p.update({k: v for k, v in payload.items() if k in p or k in ("to", "subject", "body", "content", "code")})
    ts = db.now()
    label = ACTION_LABELS.get(row["action"], row["action"])

    update = ""   # Phase 2: the line a decision adds to its waiting task, when it differs from the memory note
    if row["action"] == "decision" and decision == "reject" and not answer.strip():
        # Dismissed without choosing: nothing was decided, so the brain records nothing.
        status, result = "rejected", ""
        note = f"Owner dismissed decision #{approval_id} (\"{p.get('question', '')[:200]}\") without choosing."
        update = (f"Owner dismissed decision #{approval_id} without choosing an option. Decide within your own "
                  f"authority, or ask again differently.")
        if feedback.strip():
            note += f" Owner's note: {feedback.strip()}"
            update += f" Owner's note: {feedback.strip()}"
    elif row["action"] == "decision":
        status, result = "answered", answer.strip() or feedback.strip() or "(no answer)"
        if decided_by == "deadline":
            update = f"No answer by the deadline, so decision #{approval_id} took the recommended option: {result}"
        else:
            update = f"Owner decided #{approval_id}: {result}"
        if feedback.strip() and feedback.strip() != result:
            update += f". Owner's note: {feedback.strip()}"
        note = f"{update} (decision: \"{p.get('question', '')[:200]}\")"
    elif row["action"] == "question":
        status, result = "answered", answer or feedback or "(no answer)"
        note = f"Owner answered question #{approval_id} (\"{p.get('question', '')[:200]}\"): {result}"
    elif decision == "approve":
        db.update("approvals", approval_id, status="running", payload=json.dumps(p), decided_at=ts, feedback=feedback)
        try:
            result = execute(company, row["action"], p, approval_id=approval_id)
            status = "done"
        except Exception as e:
            result, status = str(e), "failed"
        proposer = (db.get_agent(row["agent_id"]) or {}).get("name", "") if row["agent_id"] else ""
        audit(company["id"], row["action"], proposer, "owner", p, result, approval_id)
        note = f"Owner APPROVED request #{approval_id} ({label}: {_summary(row['action'], p)}). Outcome: {result}"
    else:
        status, result = "rejected", ""
        note = f"Owner REJECTED request #{approval_id} ({label}: {_summary(row['action'], p)})."
    if feedback and row["action"] not in ("question", "decision"):
        note += f" Owner feedback: {feedback}"

    fields = {"status": status, "result": result, "payload": json.dumps(p), "decided_at": ts, "feedback": feedback,
              "decided_by": decided_by,
              "summary": _summary(row["action"], p) if row["action"] not in ("question", "decision") else row["summary"]}
    if row["action"] in ("question", "decision"):
        # Phase 2: the owner (dashboard or phone) and the scheduler's deadline can answer the same row at the same
        # moment. Only the first answer counts, so the brain records it and its origin acts on it once.
        with db._lock:
            db.run(f"UPDATE approvals SET {', '.join(f'{k}=?' for k in fields)} WHERE id=? AND status='pending'",
                   [*fields.values(), approval_id])
            claimed = db.scalar("SELECT changes()")
        if not claimed:
            now = (db.one("SELECT status FROM approvals WHERE id=?", (approval_id,)) or {}).get("status", "decided")
            raise ValueError(f"Request #{approval_id} is already {now}")
    else:
        db.update("approvals", approval_id, **fields)
    if row["action"] == "decision":
        from . import decisions   # imported here: decisions imports tools, which imports this module
        try:
            fresh = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
            if status == "answered":
                decisions.on_decided(fresh, result, decided_by)
            else:
                decisions.on_dismissed(fresh)
        except Exception as e:   # the brain record is a view; the owner's answer must stand
            print(f"decision record failed: {e}")
    db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
           (company["id"], row["agent_id"], "decision", note, 0, ts))
    if row["task_id"]:
        t = db.get_task(row["task_id"])
        if t and t["status"] in ("waiting", "blocked"):
            db.update("tasks", t["id"], status="todo", updated_at=ts, followup_at=ts,   # Phase 3: dispatched first
                      description=(t["description"] or "") + f"\n\n[Update] {update or note}")
    agent = db.get_agent(row["agent_id"]) if row["agent_id"] else None
    if status == "rejected" and feedback.strip() and agent and agent["enabled"] and row["action"] != "decision":
        # Rejected with feedback: the agent gets a task to revise and resubmit.
        details = json.dumps(p, ensure_ascii=False, indent=1)[:3000]
        db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, created_by,"
               " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (company["id"], f"Revise: {_summary(row['action'], p)[:120]}",
                f"The owner rejected request #{approval_id} with this feedback:\n\"{feedback.strip()}\"\n\n"
                f"Original request ({label}):\n{details}\n\nRevise it to address the feedback and submit it again.",
                agent["role"], agent["id"], "todo", 1, "owner", ts, ts))
        bus.notify("tasks", company_id=company["id"])
    if row["action"] == "question" and (p.get("pattern") or p.get("options")):
        from . import watchdog
        try:
            watchdog.resolve(db.one("SELECT * FROM approvals WHERE id=?", (approval_id,)), result)
        except NotImplementedError:
            pass
    if row["action"] not in ("question", "decision") and status in ("done", "failed", "rejected"):
        from . import trust   # Phase 3: clean approvals build the streak; edits reset it; rejections demote
        try:
            trust.after_decision(db.one("SELECT * FROM approvals WHERE id=?", (approval_id,)), decision,
                                 edited=bool(payload) and p != db.loads(row["payload"], {}))
        except Exception as e:   # the ladder is advice; the owner's decision stands
            print(f"trust update failed: {e}")
    _reopen_waiting(company["id"], approval_id, ts)
    _records_changed(company["id"])
    db.update("companies", company["id"], cycle_requested=1)
    kind = "decision" if status != "failed" else "error"
    bus.emit(company["id"], kind, note, row["agent_id"], {"approval_id": approval_id, "status": status})
    bus.notify("approvals")
    return db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))


def _same_question(a, b) -> bool:
    """Phase 2: decisions are deduplicated on the question text (case and spacing ignored)."""
    def norm(s):
        return " ".join(str(s or "").lower().split())
    return bool(norm(a)) and norm(a) == norm(b)


_WAITING_ON_RE = re.compile(r"Waiting on the owner: #(\d+)")


def _reopen_waiting(company_id: int, approval_id: int, ts: str) -> None:
    """Phase 0.5: after any owner decision, tasks of the company that wait with no pending approval of their own
    go back to the queue. A task whose question was refused as a duplicate waits on someone else's question and
    was never reopened before. One whose last handoff names a question that is still pending ("Waiting on the
    owner: #N", written by finish) keeps waiting for that one."""
    try:
        pending = {r["id"]: r["task_id"] for r in
                   db.q("SELECT id, task_id FROM approvals WHERE company_id=? AND status IN ('pending','running')",
                        (company_id,))}
        own = {tid for tid in pending.values() if tid}
        reopened = 0
        for t in db.q("SELECT id, result FROM tasks WHERE company_id=? AND status='waiting'", (company_id,)):
            if t["id"] in own:
                continue
            named = _WAITING_ON_RE.findall(t["result"] or "")
            if named and int(named[-1]) in pending:
                continue
            note = f"[Holdco] The owner answered #{approval_id}; continue."
            db.update("tasks", t["id"], status="todo", updated_at=ts, followup_at=ts,   # Phase 3: dispatched first
                      result=((t["result"] or "") + "\n" + note).strip()[-6000:])
            reopened += 1
        if reopened:
            bus.notify("tasks", company_id=company_id)
    except Exception as e:   # reopening is a convenience; it must never undo the owner's decision
        print(f"reopening waiting tasks failed: {e}")


def _records_changed(company_id: int) -> None:
    """Phase 0.5: records/ is rewritten from the database after every outbound action or decision."""
    try:
        from . import truth   # imported here: truth imports tools, which imports this module
        c = db.get_company(company_id)
        if c:
            truth.records_changed(c)
    except Exception as e:   # records are a view; a failure must never undo or block the action
        print(f"records refresh failed: {e}")


def audit(company_id: int, action: str, proposed_by: str, approved_by: str, payload: dict, result: str,
          approval_id: int | None = None) -> int:
    """Append-only record of every outbound action that actually ran: who proposed it, who allowed it, what was sent.
    records/ is refreshed after the row is written, so it includes this action."""
    from . import vault   # imported here: vault imports db; a secret in a result never reaches the audit log
    aid = db.run("INSERT INTO audit(created_at, company_id, action, proposed_by, approved_by, payload, result, "
                 "approval_id) VALUES(?,?,?,?,?,?,?,?)",
                 (db.now(), company_id, action, proposed_by or "", approved_by,
                  vault.redact(json.dumps(payload, sort_keys=True, default=str))[:20000],
                  vault.redact(str(result or ""))[:4000], approval_id))
    _records_changed(company_id)
    return aid


# ---------------- executors ----------------

def execute(company: dict, action: str, p: dict, approval_id: int | None = None) -> str:
    """Run one outbound action. approval_id is the request being run, when it is one (decide passes it)."""
    if action == "connector_call":
        from . import connectors   # imported here: connectors imports tools, which imports this module
        # The request's company goes along, so the connector it names is checked to still be this company's.
        return connectors.execute(p, company=company, approval_id=approval_id)
    if action == "send_email":
        in_reply = ""
        if p.get("reply_to_email_id"):
            src = db.one("SELECT message_id FROM emails WHERE id=? AND company_id=?",
                         (int(p["reply_to_email_id"]), company["id"]))
            in_reply = (src or {}).get("message_id") or ""
            db.run("UPDATE emails SET status='replied' WHERE id=?", (int(p["reply_to_email_id"]),))
        return emailer.send(company, p["to"], p["subject"], p["body"], in_reply)
    if action == "social_post":
        return _social(company, p)
    if action == "deploy_site":
        return _deploy_netlify(company)
    if action == "http_post":
        return _http(p)
    if action == "run_code":
        return run_code(company, p.get("language", "python"), p.get("code", ""), int(p.get("timeout", 60)))
    if action == "spend_money":
        return "Approved. The owner will complete this purchase themselves and record it."
    raise ValueError(f"Unknown action {action}")


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": web.UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace")


def _social(company: dict, p: dict) -> str:
    cs = db.company_settings(company)
    content = p.get("content", "")
    # Phase 1: a healthy Buffer connection with a channel for this platform queues the post there. A Buffer failure
    # after that point is a real failure (the approval shows it and can be retried), not a silent fallback.
    try:
        from . import connectors   # imported here: connectors imports tools, which imports this module
        buffer = connectors.instance("buffer", company)
    except Exception:
        buffer = None
    note = ""
    if buffer and buffer.get("enabled", 1) and buffer.get("status") == "ok":
        from . import conn_buffer
        platform = p.get("platform", "") or "social"
        if not conn_buffer.default_channels(buffer):
            note = " Buffer was not used: its card has no default channels, so Holdco does not know where to post."
        else:
            # Channel by channel; the ones that went out are kept in the request (decide stores the payload), so a
            # retry after a partial failure skips them.
            posted = [str(x) for x in (p.get("buffer_posted") or [])]
            try:
                via = conn_buffer.post_for_platform(buffer, company, platform, content, p.get("link", ""),
                                                    posted=posted)
            finally:
                if posted:
                    p["buffer_posted"] = posted
            if via is not None:
                return via
            note = f" Buffer was not used: none of its default channels is a {platform} channel."
    if cs.get("webhook_url"):
        status, text = _post_json(cs["webhook_url"], {"type": "social_post", "company": company["name"],
                                                      "platform": p.get("platform", ""), "content": content,
                                                      "link": p.get("link", "")})
        if status >= 400:
            raise RuntimeError(f"Webhook returned HTTP {status}: {text[:300]}")
        return f"Sent to the posting webhook (HTTP {status}).{note}"
    intent = "https://twitter.com/intent/tweet?" + urllib.parse.urlencode({"text": content[:280]})
    return (f"Approved. No posting webhook is set up, so the owner posts it by hand. One-click X link: {intent}"
            f"{note}")


def _http(p: dict) -> str:
    url = web.check_public_url(p["url"])
    method = (p.get("method") or "POST").upper()
    body = p.get("body")
    data = (json.dumps(body) if isinstance(body, (dict, list)) else str(body or "")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": web.UA}
    headers.update({k: str(v) for k, v in (p.get("headers") or {}).items()})
    req = urllib.request.Request(url, data=data if method != "GET" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"HTTP {r.status}\n{r.read(8000).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}\n{e.read(3000).decode('utf-8', 'replace')}"


def _deploy_netlify(company: dict) -> str:
    cs = db.company_settings(company)
    site_dir = workspace.safe_path(company, "site")
    if not (site_dir / "index.html").exists():
        raise RuntimeError("site/index.html doesn't exist yet.")
    token = cs.get("netlify_token")
    if not token:
        return ("Approved, but no Netlify token is set (company Settings). The site is ready in the site/ "
                "folder and visible in the local preview.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in site_dir.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(site_dir).as_posix())
    auth = {"Authorization": f"Bearer {token}", "User-Agent": web.UA}
    site_id = cs.get("netlify_site_id")
    if not site_id:
        status, text = _post_json("https://api.netlify.com/api/v1/sites", {"name": f"{company['slug']}-{int(time.time()) % 100000}"}, auth)
        if status >= 400:
            raise RuntimeError(f"Netlify couldn't create a site: HTTP {status} {text[:300]}")
        site = json.loads(text)
        site_id = site["id"]
        cs["netlify_site_id"] = site_id
        cs["site_url"] = site.get("ssl_url") or site.get("url", "")
        # Only these keys: cs holds the resolved Netlify token, which must never go back into the table.
        db.update_company_settings(company["id"], netlify_site_id=site_id, site_url=cs["site_url"])
    req = urllib.request.Request(f"https://api.netlify.com/api/v1/sites/{site_id}/deploys", data=buf.getvalue(),
                                 method="POST", headers={**auth, "Content-Type": "application/zip"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Netlify deploy failed: HTTP {e.code} {e.read(300).decode('utf-8', 'replace')}") from None
    url = info.get("ssl_url") or info.get("url") or cs.get("site_url", "")
    if url:
        db.update_company_settings(company["id"], site_url=url)
    return f"Website deployed: {url}"


def run_code(company: dict, language: str, code: str, timeout: int = 60, network: bool = False) -> str:
    from . import sandbox   # Phase 3: inside a container when Docker or Podman is available
    boxed = sandbox.run(company, language, code, timeout, network=network)
    if boxed is not None:
        return boxed
    ws = workspace.root(company)
    runs = ws / ".runs"
    runs.mkdir(parents=True, exist_ok=True)
    timeout = max(5, min(int(timeout or 60), 600))
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("HOLDCO", "OLLAMA"))}
    env["PYTHONIOENCODING"] = "utf-8"
    if language in ("python", "py"):
        f = runs / f"run_{int(time.time() * 1000)}.py"
        f.write_text(code, encoding="utf-8")
        cmd = [sys.executable, str(f)]
    elif os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
    else:
        cmd = ["bash", "-c", code]
    try:
        proc = subprocess.run(cmd, cwd=str(ws), capture_output=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except FileNotFoundError as e:
        return f"Couldn't start the interpreter: {e}"
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    res = f"Exit code {proc.returncode}\n"
    if out:
        res += f"--- stdout ---\n{out[-6000:]}\n"
    if err:
        res += f"--- stderr ---\n{err[-3000:]}\n"
    return res


def retry(approval_id: int) -> dict:
    """Put a failed request back in the inbox (e.g. after fixing email settings)."""
    row = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not row or row["status"] != "failed":
        raise ValueError("Only failed requests can be retried.")
    db.update("approvals", approval_id, status="pending", result="", decided_at=None)
    bus.notify("approvals")
    return db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
