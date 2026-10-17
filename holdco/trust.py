"""The trust ladder (Phase 3, docs/phases/phase-3-verify.md section 6): per company and action, level 0 asks, level 1
runs on its own up to a daily cap, level 2 runs on its own. Clean approvals build a streak; at 15 the owner is asked
whether to promote; an edit resets the streak and a rejection demotes.

gate_policy() is the one place that decides, for actions.gate, whether taint or the ladder overrides the configured
policy (section 6's order, first match wins):
1. spend_money and irreversible always ask (locked, as before);
2. a policy of 'off' is refused (actions.gate checks it before asking here);
3. a session that read outside content asks for the outward actions in TAINTED_ACTIONS (taint.py);
4. the never-promoted cases ask: an email that reads like a legal commitment (LEGAL_RE), the first email to a person;
5. level 2 runs;
6. level 1 runs while today's runs by the ladder are below cap_per_day, then asks ("daily cap reached");
7. otherwise the policy applies as before.
Only the actions in LADDER climb; run_code keeps its policy (the sandbox and its approval card cover it).

Table trust(company_id, action, level, streak, cap_per_day, updated_at); a missing row is level 0, streak 0. The
audit log records a run the ladder allowed with approved_by "trust:<level>".
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re

from . import bus, config, db
from .routing import HTTPError, company_or_404, route, to_int

ASK, CAPPED, AUTO = 0, 1, 2
LEVELS = {ASK: "Ask", CAPPED: "Auto with a daily cap", AUTO: "Auto"}
PROMOTE_AT = 15            # clean approvals in a row before a promotion is suggested
NO_AGAIN_DAYS = 7          # a "No" to a promotion is not asked again for this long
DECISION_HOURS = 7 * 24    # the promotion decision's deadline; unanswered, it takes the recommended level 1
MAX_CAP = 1000

# The actions that climb, with the daily cap a promotion suggests (send_email: the company's daily_email_limit).
LADDER = ("send_email", "social_post", "deploy_site", "http_post", "connector_write", "connector_publish",
          "connector_contact")
DEFAULT_CAPS = {"social_post": 5, "deploy_site": 3, "http_post": 10, "connector_write": 20, "connector_publish": 5,
                "connector_contact": 10}
# For the promotion question: "Let agents <label> on their own, up to N a day?"
LABELS = {"send_email": "send emails", "social_post": "publish social posts", "deploy_site": "deploy the website",
          "http_post": "send data to outside services", "connector_write": "change data in connected services",
          "connector_publish": "publish through connected services",
          "connector_contact": "message people through connected services"}
# Outward actions a session that read outside content may not take on its own (section 5).
TAINTED_ACTIONS = LADDER + ("spend_money",)
# The spec's pattern, with plurals: "the contracts" and "our warranties" are legal commitments as much as the singular.
LEGAL_RE = re.compile(r"\b(contracts?|agreements?|guarantee[ds]?|price guarantees?|terms of service|warrant(?:y|ies)|"
                      r"indemnif\w*|liabilit\w*|sign (the|this))\b", re.I)
TAINTED = "asked because this session read outside content"
CAP_REACHED = "daily cap reached"


def _today_start() -> str:
    return db.now()[:10] + "T00:00:00Z"   # the audit log's day is the UTC day, like the email cap


def _ago(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_cap(company: dict, action: str) -> int:
    if action == "send_email":
        return max(1, to_int(db.company_settings(company).get("daily_email_limit"), 20))
    return DEFAULT_CAPS.get(action, 5)


def get(company_id: int, action: str) -> dict:
    """The ladder row of an action (level 0, streak 0, no cap when there is none yet)."""
    row = db.one("SELECT * FROM trust WHERE company_id=? AND action=?", (company_id, action))
    return row or {"company_id": company_id, "action": action, "level": ASK, "streak": 0, "cap_per_day": 0,
                   "updated_at": None}


def _save(company_id: int, action: str, level: int, streak: int, cap: int) -> None:
    db.run("INSERT INTO trust(company_id, action, level, streak, cap_per_day, updated_at) VALUES(?,?,?,?,?,?) "
           "ON CONFLICT(company_id, action) DO UPDATE SET level=excluded.level, streak=excluded.streak, "
           "cap_per_day=excluded.cap_per_day, updated_at=excluded.updated_at",
           (company_id, action, int(level), int(streak), int(cap), db.now()))


def _policy_of_call(payload: dict) -> str:
    from . import connectors   # imported here: connectors imports tools, which imports this module
    return connectors.policy_for(str(payload.get("tag") or "")) or ""


def ladder_action(row: dict) -> str:
    """The ladder an approvals or audit row counts toward: its action, or for a connector call its tag's policy."""
    if row.get("action") != "connector_call":
        return row.get("action") or ""
    return _policy_of_call(db.loads(row.get("payload"), {}) or {})


def used_today(company_id: int, action: str) -> int:
    """Runs of this action today that the ladder allowed without asking (audit rows approved by trust:<level>)."""
    since = _today_start()
    if not action.startswith("connector_"):
        return db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=? AND action=? AND approved_by LIKE 'trust:%' "
                         "AND created_at>=?", (company_id, action, since)) or 0
    rows = db.q("SELECT action, payload FROM audit WHERE company_id=? AND action='connector_call' AND approved_by "
                "LIKE 'trust:%' AND created_at>=?", (company_id, since))
    # A payload cut short in the log cannot say its tag: it counts, so the cap errs on the side of asking.
    return sum(1 for r in rows if db.loads(r["payload"], None) is None or ladder_action(r) == action)


def _first_contact(company_id: int, to: str) -> bool:
    """No contact with this address has a last touch, or the company never exchanged an email with them: the email
    would be the first message to a new person. last_touch alone is not enough, since agents write it themselves
    (update_contact), and a rule that never lets an email go on its own must not hang on what the agent says."""
    a = to.strip().lower()
    known = db.one("SELECT id FROM contacts WHERE company_id=? AND lower(email)=? AND COALESCE(last_touch, '')!='' "
                   "LIMIT 1", (company_id, a))
    return not known or not _exchanged(company_id, a)


def _exchanged(company_id: int, a: str) -> bool:
    """The company emailed this address (a sent email, or a send in the audit log) or received an email from it."""
    if db.one("SELECT id FROM emails WHERE company_id=? AND direction='out' AND lower(to_addr)=? LIMIT 1",
              (company_id, a)):
        return True
    like = f"%{a}%"
    if any(email.utils.parseaddr(r["from_addr"] or "")[1].strip().lower() == a for r in db.q(
            "SELECT from_addr FROM emails WHERE company_id=? AND direction='in' AND lower(from_addr) LIKE ?",
            (company_id, like))):
        return True
    return any(str((db.loads(r["payload"], {}) or {}).get("to") or "").strip().lower() == a for r in db.q(
        "SELECT payload FROM audit WHERE company_id=? AND action='send_email' AND result NOT LIKE 'FAILED%' "
        "AND lower(payload) LIKE ?", (company_id, like)))


def never_promoted(company: dict, action: str, payload: dict) -> str:
    """The reason a send_email asks whatever its level (a legal commitment, a first message), or ''."""
    if action != "send_email":
        return ""
    m = LEGAL_RE.search(f"{payload.get('subject') or ''}\n{payload.get('body') or ''}")
    if m:
        return (f"asked because the email reads like a legal commitment ('{m.group(0)}'); those are never sent "
                f"without you")
    to = str(payload.get("to") or "").strip()
    if to and _first_contact(company["id"], to):
        return f"asked because it is the first email to {to}; first messages to a new person are never sent without you"
    return ""


def gate_policy(company: dict, action: str, payload: dict, policy: str, taint_sources: list[str]) -> tuple[str, str]:
    """actions.gate: the policy to apply ('approve', 'auto' or 'off') and why. With 'approve', why is the reason shown
    on the Inbox card when taint or the ladder overrides the configured policy; with 'auto', it is "trust:<level>"
    when the ladder let the action run (the audit row's approved_by). '' otherwise. action is the policy's action
    (connector calls pass their tag's policy)."""
    if action in config.LOCKED_POLICIES:
        return "approve", ""                      # 1. money and irreversible actions always ask
    if policy == "off":
        return policy, ""                         # 2. refused
    if taint_sources and action in TAINTED_ACTIONS:
        return "approve", f"{TAINTED}: {', '.join(taint_sources)}"   # 3. outside content may be steering this
    why = never_promoted(company, action, payload)
    if why:
        return "approve", why                     # 4. never on its own, whatever the level or the policy
    if action not in LADDER:
        return policy, ""
    row = get(company["id"], action)
    level = to_int(row["level"])
    if level >= AUTO:
        return "auto", f"trust:{AUTO}"            # 5.
    if level == CAPPED:                           # 6.
        cap = to_int(row["cap_per_day"]) or default_cap(company, action)
        used = used_today(company["id"], action)
        if used < cap:
            return "auto", f"trust:{CAPPED}"
        return "approve", f"{CAP_REACHED}: {used} of {cap} ran today without asking (trust level 1)"
    return policy, ""                             # 7. the policy as before


def note_sent(company_id: int, payload: dict) -> None:
    """An email went out: the contacts with that address have a last touch now, so the next email to them is not a
    first message."""
    to = str((payload or {}).get("to") or "").strip().lower()
    if to:
        db.run("UPDATE contacts SET last_touch=? WHERE company_id=? AND lower(email)=?",
               (db.now()[:10], company_id, to))


def after_decision(row: dict, decision: str, edited: bool) -> None:
    """actions.decide on a request: bump or reset the streak, demote on a rejection, suggest a promotion."""
    if not row:
        return
    action = ladder_action(row)
    if action == "send_email" and row.get("status") == "done":
        note_sent(row["company_id"], db.loads(row.get("payload"), {}) or {})
    if action not in LADDER:
        return
    cid = row["company_id"]
    with db._lock:   # the dashboard and the phone can decide two requests at the same moment
        cur = get(cid, action)
        level, streak = to_int(cur["level"]), to_int(cur["streak"])
        if decision == "approve":
            streak = 0 if edited else streak + 1
        else:
            level, streak = max(ASK, level - 1), 0
        _save(cid, action, level, streak, to_int(cur["cap_per_day"]))
    if level < to_int(cur["level"]):
        bus.emit(cid, "info", f"Trust lowered: agents {LABELS[action]} at level {level} ({LEVELS[level].lower()}) "
                              f"after you rejected request #{row['id']}.", None, {"action": action, "level": level})
    if streak >= PROMOTE_AT and level < AUTO:
        suggest_promotion(cid, action, streak)


def _trust_decisions(company_id: int, action: str, where: str, params: tuple = ()) -> list[dict]:
    rows = db.q(f"SELECT * FROM approvals WHERE company_id=? AND action='decision' AND {where} ORDER BY id DESC",
                (company_id, *params))
    out = []
    for r in rows:
        origin = (db.loads(r["payload"], {}) or {}).get("origin") or {}
        if isinstance(origin, dict) and origin.get("kind") == "trust" and origin.get("action") == action:
            out.append(r)
    return out


def _declined_recently(company_id: int, action: str) -> bool:
    """The owner said "No" to this promotion (or dismissed it) within the last week."""
    for r in _trust_decisions(company_id, action, "status IN ('answered','rejected') AND decided_at>=?",
                              (_ago(NO_AGAIN_DAYS),)):
        if r["status"] == "rejected" or not str(r["result"] or "").strip().lower().startswith("yes"):
            return True
    return False


def suggest_promotion(company_id: int, action: str, streak: int = PROMOTE_AT) -> str:
    """Ask the owner whether to promote an action that earned it: a reversible decision whose deadline takes level 1.
    Not asked while one is pending, or within a week of a "No". Returns the gate's text, or '' when not asked."""
    company = db.get_company(company_id)
    if not company or action not in LADDER or to_int(get(company_id, action)["level"]) >= AUTO:
        return ""
    if _trust_decisions(company_id, action, "status='pending'") or _declined_recently(company_id, action):
        return ""
    from . import decisions   # imported here: decisions imports tools, which imports this module
    cap = to_int(get(company_id, action)["cap_per_day"]) or default_cap(company, action)
    options = [f"Yes, up to {cap} a day", "Yes, always", "No, keep asking"]
    context = (f"You approved {streak} of these in a row without changing them. \"Yes, up to {cap} a day\" lets them "
               f"run on their own until {cap} ran in a day, then they ask again. Legal commitments, first emails to "
               f"a new person and anything after agents read outside content still ask. You can change this any time "
               f"in the company's Trust settings.")
    return decisions.create(company, {"id": None, "name": "Holdco"}, None,
                            f"Let agents {LABELS[action]} on their own, up to {cap} a day?", options, options[0],
                            deadline_hours=DECISION_HOURS, reversible=True, context=context,
                            origin={"kind": "trust", "action": action, "cap": cap})


def apply_promotion(company_id: int, origin: dict, answer: str) -> None:
    """decisions.on_decided for origin kind "trust": "Yes, up to N a day" sets level 1 with that cap, "Yes, always"
    level 2; "No, keep asking" changes nothing (and the question waits a week). A promotion never lowers a level the
    owner raised in the meantime."""
    action = str((origin or {}).get("action") or "")
    said = str(answer or "").strip().lower()
    if action not in LADDER or not said.startswith("yes"):
        return
    with db._lock:
        cur = get(company_id, action)
        n = re.search(r"up to (\d+)", said)
        cap = to_int(n.group(1)) if n else to_int((origin or {}).get("cap"))
        if cap <= 0:
            cap = to_int(cur["cap_per_day"]) or default_cap(db.get_company(company_id) or {}, action)
        level = max(to_int(cur["level"]), AUTO if "always" in said else CAPPED)
        _save(company_id, action, level, 0, min(cap, MAX_CAP))
    bus.emit(company_id, "info", f"Trust raised: agents {LABELS[action]} on their own"
                                 + (f", up to {cap} a day." if level == CAPPED else "."), None,
             {"action": action, "level": level})
    bus.notify("trust", company_id=company_id)


# ---------------------------------------------------------------- dashboard API

def _rows(company: dict) -> list[dict]:
    """Every gated action with its level, streak and cap; locked actions always ask, run_code follows its policy."""
    cid = company["id"]
    stored = {r["action"]: r for r in db.q("SELECT * FROM trust WHERE company_id=?", (cid,))}
    out = []
    for action, label in config.POLICY_ACTIONS.items():
        r = stored.get(action) or get(cid, action)
        locked = action in config.LOCKED_POLICIES
        on = action in LADDER
        level = to_int(r["level"]) if on else ASK
        item = {"action": action, "label": label, "level": level, "level_name": LEVELS[level],
                "streak": to_int(r["streak"]) if on else 0,
                "cap_per_day": (to_int(r["cap_per_day"]) or default_cap(company, action)) if on else 0,
                "locked": locked, "on_ladder": on, "updated_at": r.get("updated_at")}
        if locked:
            item["note"] = "always asks"
        elif not on:
            item["note"] = "follows its policy"
        else:
            item["used_today"] = used_today(cid, action)
        out.append(item)
    return out


@route("GET", r"/api/companies/(\d+)/trust")
def api_trust(h, m, body):
    c = company_or_404(m.group(1))
    return {"company_id": c["id"], "actions": _rows(c), "levels": {str(k): v for k, v in LEVELS.items()},
            "promote_at": PROMOTE_AT}


def _whole(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit())):
        raise HTTPError(400, f"{what} must be a whole number from {lo} to {hi}.")
    n = int(v)
    if not lo <= n <= hi:
        raise HTTPError(400, f"{what} must be a whole number from {lo} to {hi}.")
    return n


@route("PUT", r"/api/companies/(\d+)/trust")
def api_trust_put(h, m, body):
    """The owner sets an action's level and daily cap: {"action", "level", "cap_per_day"}; it applies at once."""
    c = company_or_404(m.group(1))
    if not isinstance(body, dict):
        raise HTTPError(400, "Send a JSON object with action, level and cap_per_day.")
    action = body.get("action")
    if not isinstance(action, str) or action not in config.POLICY_ACTIONS:
        raise HTTPError(400, f"Unknown action '{action}'. Actions: {', '.join(LADDER)}.")
    label = config.POLICY_ACTIONS[action]
    if action in config.LOCKED_POLICIES:
        raise HTTPError(400, f"'{label}' always asks; it is not on the trust ladder.")
    if action not in LADDER:
        raise HTTPError(400, f"'{label}' is not on the trust ladder; set its policy in Settings instead.")
    if "level" not in body and "cap_per_day" not in body:
        raise HTTPError(400, "Give level (0 ask, 1 auto with a daily cap, 2 auto) or cap_per_day.")
    cur = get(c["id"], action)
    level = _whole(body["level"], "level", ASK, AUTO) if "level" in body else to_int(cur["level"])
    cap = (_whole(body["cap_per_day"], "cap_per_day", 1, MAX_CAP) if "cap_per_day" in body
           else to_int(cur["cap_per_day"]) or default_cap(c, action))
    with db._lock:
        _save(c["id"], action, level, to_int(get(c["id"], action)["streak"]), cap)
    bus.emit(c["id"], "info", f"The owner set '{label}' to trust level {level} ({LEVELS[level].lower()})"
                              + (f", up to {cap} a day." if level == CAPPED else "."), None,
             {"action": action, "level": level, "cap_per_day": cap})
    bus.notify("trust", company_id=c["id"])
    return api_trust(h, m, body)
