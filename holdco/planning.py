"""Owner questions and planning effort (Phase 0.5): one open question per topic, structured questions, the change
stamp and backoff that stop idle re-planning, the daily token budget, charter validation, the planning header and
the staged founding state machine that agents.py drives.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, sections 3 and 4.

Why it exists, from the owner's first real run (Rooforyou, 13 hours on gpt-oss:20b, 9.6 million tokens): 84% of
the tokens went to CEO planning cycles. The scheduler re-planned every 10 minutes whenever no task was queued,
although everything was waiting on the owner; the CEO asked the owner the same deploy-and-outreach question 38
times; founding spent its whole budget on research and created no tasks; the charter only existed as a file and
had no price, no competitors and no demand plan.

Company settings this module keeps (beside the documented ones): last_waiting_notice and budget_notice (local
dates of the once-a-day notices), founding_retry (the stage being retried once) and founding_research_note (how
research ended when it ended early, for the charter stage). companies.last_cycle_stamp
holds a small JSON record: {"s": change stamp when the last cycle started (or was absorbed), "run": id of the
last cycle run whose own changes were absorbed}.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
import traceback
import zlib

from . import actions, bus, db, emailer, playbooks, workspace
from .routing import route, company_or_404
from .tools import Ctx, REGISTRY

_settings_lock = threading.Lock()


def _today() -> str:
    """The PC's local date: budgets and once-a-day notices follow the owner's day, not UTC."""
    return _dt.date.today().isoformat()


def update_company_settings(company_id: int, **values) -> dict:
    """Merge values into a company's stored settings JSON (only the stored keys, never the defaults)."""
    with _settings_lock:
        row = db.one("SELECT settings FROM companies WHERE id=?", (company_id,))
        if not row:
            return {}
        stored = db.loads(row.get("settings"), {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        stored.update(values)
        db.update("companies", company_id, settings=json.dumps(stored))
        return stored


def _age(created_at: str | None) -> str:
    minutes = db.minutes_since(created_at)
    if minutes >= 1e8:
        return "a while"
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''}"
    if minutes < 48 * 60:
        n = int(minutes // 60)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = int(minutes // (24 * 60))
    return f"{n} day{'s' if n != 1 else ''}"


# =============================================================== 3.1 one open question per topic

MAX_PENDING_QUESTIONS = 3      # agent questions waiting per company; a fourth is refused
MAX_OPTIONS = 6
CONTEXT_CAP = 1500
OVERLAP_THRESHOLD = 0.5        # Jaccard similarity of content words that counts as the same question
GENERIC_OVERLAP = 0.25         # the lower bar for questions that share only a generic topic

# Topic -> (label shown to agents, keywords). One question can carry several topics ("deploy and outreach");
# a shared topic with a pending question means the owner already has it. Putting the website online, making it
# public or hosting it all mean deploying; "online" and "public" count only after the site or "put it", so
# "Which online communities?" is not a deploy question.
TOPICS: dict[str, tuple[str, re.Pattern]] = {
    "deploy": ("deploy", re.compile(
        r"\b(deploy\w*|publish\w*|netlify|vercel|go(ing)? live|launch(ing)? the (site|website|page|landing page)|"
        r"put (the |our )?(site|website|landing page|page)|hosting|host(s|ed)? (the |our )?(site|website|page|"
        r"landing page|it)|hosted on|go(ing)? online|(put|make|take|bring|get) (it|them) (online|public|live)|"
        r"(site|website|page)\b[^.?!\n]{0,30}\b(online|public(ly)?))\b", re.I)),
    "email": ("outreach", re.compile(r"\b(e-?mails?|e-?mailing|outreach|mailbox\w*|smtp|imap)\b", re.I)),
    "analytics": ("analytics", re.compile(r"\b(analytics|ga4|measurement id|tracking|google tag)\b", re.I)),
    "domain": ("domain", re.compile(r"\b(domains?|dns)\b", re.I)),
    "money": ("money", re.compile(r"\b(money|budgets?|spend\w*|spent|pay|paying|payments?|purchas\w*|buy|buying)\b",
                                  re.I)),
    "account": ("accounts", re.compile(r"\b(accounts?|credentials?|log ?in|logins?|api keys?|tokens?|passwords?)\b",
                                       re.I)),
    "pricing": ("pricing", re.compile(r"\b(pric(e|es|ed|ing)|how much to charge)\b", re.I)),
    "legal": ("legal", re.compile(r"\b(legal\w*|llc|contracts?|terms( of service)?|privacy policy|gdpr|lawyers?)\b",
                                  re.I)),
}
# Topics too broad to mean "the same question" on their own: a mailbox question and a question about the address
# the landing page shows both mention email; a Netlify account and an analytics account are different accounts.
# They match only together with a second shared topic or real word overlap (GENERIC_OVERLAP).
GENERIC_TOPICS = frozenset({"account", "email"})
# A pending request of these kinds already puts the topic in the owner's inbox.
APPROVAL_TOPICS = {"deploy_site": "deploy", "send_email": "email", "spend_money": "money"}
APPROVAL_NOUNS = {"deploy_site": "deploy", "send_email": "email", "spend_money": "purchase"}

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be been before being below between both but by can
could did do does doing done for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not now of off on once only or other our ours out over own please same she
should so some such than that the their theirs them then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you your yours yourself shall may
might must let lets us okay ok want wants need needs like get got go going yes owner question ask asking
""".split())

DO_NOT_ASK_AGAIN = ("Do not ask again. Continue with work that does not need the answer, or finish with status "
                    "'waiting'.")


def detect_topics(text: str) -> list[str]:
    """Topic keys found in a question (deploy, email, analytics, domain, money, account, pricing, legal)."""
    return [key for key, (_label, rx) in TOPICS.items() if rx.search(text or "")]


def topic_label(topics) -> str:
    labels = [TOPICS[t][0] for t in topics if t in TOPICS]
    return " and ".join(labels) if labels else "a similar question"


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def content_words(text: str) -> set[str]:
    """Lowercase words without stopwords, with a light suffix strip, for comparing two questions."""
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def parse_options(options) -> list[str]:
    """Options one per line (bullets and numbering removed), or a JSON list, deduplicated, at most six."""
    raw = options
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, (list, tuple)):
        lines = [str(x) for x in raw]
    else:
        text = str(raw or "")
        lines = text.splitlines()
        if len(lines) == 1 and (" | " in text or ";" in text):
            lines = re.split(r"\s+\|\s+|;", text)
    out: list[str] = []
    for line in lines:
        opt = _clean_option(line)
        if opt and opt not in out:
            out.append(opt[:200])
    return out[:MAX_OPTIONS]


def _clean_option(text: str) -> str:
    return re.sub(r"^\s*(?:[-*\u2022+]|\d+[.)]|[a-fA-F][.)])\s+", "", str(text or "")).strip().strip("\"'")


def _pending(company_id: int) -> list[dict]:
    rows = db.q("SELECT id, action, summary, payload, created_at FROM approvals WHERE company_id=? AND "
                "status='pending' ORDER BY id", (company_id,))
    for r in rows:
        r["p"] = db.loads(r["payload"], {}) or {}
    return rows


def _same_topic(shared: list[str], mine: set, their_text: str) -> bool:
    """Do two questions that share these topics ask the same thing? One specific topic (deploy, analytics,
    domain, money, pricing, legal) is enough; generic ones need a second shared topic or real word overlap."""
    if not shared:
        return False
    if any(t not in GENERIC_TOPICS for t in shared) or len(shared) >= 2:
        return True
    return jaccard(mine, content_words(their_text)) >= GENERIC_OVERLAP


def find_duplicate(company_id: int, question: str, topics: list[str]) -> dict | None:
    """The pending question or request that already covers this question, as {"id", "reason", "label",
    "text"}, or None. Topics are checked first, then pending requests of the same kind, then word overlap."""
    rows = _pending(company_id)
    questions = [r for r in rows if r["action"] == "question"]
    wanted = set(topics)
    mine = content_words(question)
    for r in questions:
        p = r["p"]
        their_text = str(p.get("question") or r["summary"] or "")
        theirs = set(p.get("topics") or detect_topics("\n".join([their_text]
                                                                 + [str(o) for o in p.get("options") or []])))
        shared = [t for t in topics if t in theirs]
        if _same_topic(shared, mine, their_text):
            label = topic_label(shared)
            return {"id": r["id"], "reason": "topic", "label": label,
                    "text": f"Already waiting on the owner as #{r['id']} ({label}, asked {_age(r['created_at'])} "
                            f"ago). {DO_NOT_ASK_AGAIN}"}
    for r in rows:
        topic = APPROVAL_TOPICS.get(r["action"])
        their_text = f"{r['summary'] or ''} {r['p'].get('subject') or ''}"
        if topic and topic in wanted and _same_topic([topic], mine, their_text):
            label = topic_label([topic])
            noun = APPROVAL_NOUNS[r["action"]]
            return {"id": r["id"], "reason": "approval", "label": label,
                    "text": f"Already waiting on the owner as #{r['id']} ({label}, asked {_age(r['created_at'])} "
                            f"ago): your {noun} request #{r['id']} is already in the owner's inbox. "
                            f"{DO_NOT_ASK_AGAIN}"}
    for r in questions:
        if jaccard(mine, content_words(str(r["p"].get("question") or r["summary"] or ""))) >= OVERLAP_THRESHOLD:
            return {"id": r["id"], "reason": "overlap", "label": "a similar question",
                    "text": f"Already waiting on the owner as #{r['id']} (a similar question, asked "
                            f"{_age(r['created_at'])} ago). {DO_NOT_ASK_AGAIN}"}
    return None


# The owner reads questions on a phone, often hours later, without the agent's context.
MIN_QUESTION_CHARS = 12   # "Proceed?" says nothing; "Phoenix or Tucson first?" is fine
VAGUE_RX = re.compile(r"\b(which|what|the|this|that|these|those)\s+(option|options|one|choice|approach)\b"
                      r"|\bconfirm\s+(this|that|it)\b|\b(is|are)\s+(this|that|it|these)\s+(ok|okay|fine|good|right)\b"
                      r"|\bproceed\?*$", re.I)
# Things only the owner can give: accounts, access, money, legal and identity details, decisions about them.
OWNER_ONLY_RX = re.compile(r"\b(api[ -]?key|token|password|credential|login|account|mailbox|smtp|domain|dns|"
                           r"budget|money|card|payment|postal address|address|phone number|legal|llc|contract|"
                           r"signature|approval|permission|decision|preference|brand|logo|name)\b", re.I)
# Work a team can do itself: lists, research, notes, data, drafts.
TEAM_WORK_RX = re.compile(r"\b(provide|send|share|give|supply|upload|compile|find|gather|get)\b[^.?!]{0,60}?"
                          r"\b(list|lists|file|files|notes|research|data|dataset|contacts?|leads?|emails? of|"
                          r"spreadsheet|csv|copy|content|template|document|report|interview notes|pricing info)\b",
                          re.I)


def question_problem(question: str, opts: list[str]) -> str | None:
    """A question the owner could not answer from a phone, or one the team should answer itself."""
    q = " ".join(question.split())
    if len(q) < MIN_QUESTION_CHARS or (VAGUE_RX.search(q) and len(re.findall(r"\w+", q)) < 12):
        return ("ERROR: the owner reads questions on a phone, later, without your context. Say the decision in "
                "the question itself, for example 'Should we price the starter plan at $29 or $49 a month?', and "
                "put the choices in options.")
    if TEAM_WORK_RX.search(q) and not OWNER_ONLY_RX.search(q):
        return ("ERROR: your team can gather this itself. Create a task for the right teammate with create_task "
                "instead. Ask the owner only for decisions, accounts, access, money or permission.")
    return None


def ask_owner(ctx: Ctx, question: str, options: str = "", recommended: str = "", context: str = "") -> str:
    """The ask_owner tool body: one open question per topic, at most three from agents, options and a
    recommendation as structured fields the Inbox and the phone turn into buttons."""
    question = (question or "").strip()
    if not question:
        return "ERROR: the question is empty. Write the one thing you need the owner to decide."
    problem = question_problem(question, parse_options(options))
    if problem:
        return problem
    cid = ctx.company["id"]
    opts = parse_options(options)
    rec = _clean_option(recommended)
    payload: dict = {"question": question[:2000]}
    if opts:
        payload["options"] = opts
        if rec and rec in opts:   # a recommendation that is not one of the buttons would confuse the owner
            payload["recommended"] = rec
    if (context or "").strip():
        payload["context"] = context.strip()[:CONTEXT_CAP]
    topics = detect_topics("\n".join([question] + opts))
    if topics:
        payload["topics"] = topics
    dup = find_duplicate(cid, question, topics)
    if dup:
        # The run now waits on that pending question or request: finish(status='waiting') accepts it, and the
        # owner's decision on it reopens the task (actions.decide).
        ctx.waiting_on = dup["id"]
        _deduped(ctx, question, topics, dup["reason"], dup["id"],
                 f"question not queued: already waiting as #{dup['id']} ({dup['label']})")
        return dup["text"]
    mine = [r for r in _pending(cid) if r["action"] == "question" and not r["p"].get("pattern")]
    if len(mine) >= MAX_PENDING_QUESTIONS:
        listed = "; ".join(f"#{r['id']}: {str(r['p'].get('question') or r['summary'])[:120]}" for r in mine)
        _deduped(ctx, question, topics, "cap", None,
                 f"question not queued: {len(mine)} questions are already waiting on the owner")
        return (f"ERROR: {len(mine)} questions from the team are already waiting on the owner ({listed}). Do not "
                f"queue another: continue with work that does not need an answer, or finish with status "
                f"'blocked' and say what you need from the owner. When one is answered, fold this into your next "
                f"question.")
    return actions.gate(ctx.company, ctx.agent, ctx.task["id"] if ctx.task else None, "question", payload)


def _deduped(ctx: Ctx, question: str, topics: list[str], reason: str, approval_id, message: str) -> None:
    """Count every refused question (the Bench scores how often a model repeats itself)."""
    try:
        bus.emit(ctx.company["id"], "question_deduped", message, ctx.agent.get("id"),
                 {"reason": reason, "approval_id": approval_id, "topics": topics, "question": question[:300],
                  "task_id": ctx.task["id"] if ctx.task else None})
    except Exception:
        traceback.print_exc()


# =============================================================== 3.2, 3.3 effort follows change

MAX_BACKOFF = 8          # the due interval doubles up to eight times while nothing changes
FLAG_SECONDS = 900       # how long cycle_started's "did anything change" answer stays valid for the prompt

# Events that mean something really changed. A planning cycle's own "done" event has no task id and is left
# out, otherwise every cycle would trigger the next one. Agents' file writes and task edits are not news either:
# what they change shows up in the task counts.
REAL_CHANGE_SQL = ("(kind IN ('decision','email','connector','fact','facts') "
                   "OR (kind='done' AND data NOT LIKE '%\"task_id\": null%') "
                   "OR (kind='file' AND data LIKE '%\"by\": \"owner\"%') "
                   "OR (kind='metric' AND agent_id IS NULL))")
TASK_STATUSES = ("todo", "in_progress", "waiting", "blocked", "done", "cancelled")
# How a planning cycle can end without a usable result. Such a cycle is no baseline: the next idle or due
# trigger plans again (one failed cycle once froze an idle company for good).
FAILED_CYCLE = ("error", "interrupted", "stopped", "out_of_steps")
# Company settings that change what the team can do (a mailbox, a Netlify token, the site address, the postal
# address outreach needs, the owner's standing orders, the business type). A change to any of them is news.
CAPABILITY_KEYS = ("site_url", "webhook_url", "postal_address", "directives", "archetype")

# company id -> (when, changed, owner requested) set by cycle_started and used once by cycle_header
_cycle_flags: dict[int, tuple[float, bool, bool]] = {}


def task_counts(company_id: int) -> dict[str, int]:
    counts = {s: 0 for s in TASK_STATUSES}
    for r in db.q("SELECT status, COUNT(*) n FROM tasks WHERE company_id=? GROUP BY status", (company_id,)):
        counts[r["status"]] = r["n"]
    return counts


def capability_hash(cs: dict) -> str:
    """A short hash of the capability settings (secrets only as set or not set), so a settings change counts as
    a change for planning and the owner's settings form can tell whether anything that matters changed."""
    cs = cs or {}
    parts = ["smtp" if emailer.smtp_configured(cs) else "", "imap" if emailer.imap_configured(cs) else "",
             "netlify" if cs.get("netlify_token") else ""]
    parts += [str(cs.get(k) or "").strip() for k in CAPABILITY_KEYS]
    return format(zlib.crc32("\x1f".join(parts).encode("utf-8", "replace")), "08x")


def change_stamp(company: dict) -> str:
    """A cheap fingerprint of everything that should make the CEO plan again: the newest meaningful event, the
    newest owner chat message, the number of pending approvals, the tasks per status and the capability
    settings."""
    cid = company["id"]
    ev = db.one(f"SELECT id FROM events WHERE company_id=? AND {REAL_CHANGE_SQL} ORDER BY id DESC LIMIT 1", (cid,))
    msg = db.one("SELECT id FROM messages WHERE company_id=? AND sender='owner' ORDER BY id DESC LIMIT 1", (cid,))
    pending = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pending'", (cid,)) or 0
    counts = task_counts(cid)
    return (f"e{ev['id'] if ev else 0}.m{msg['id'] if msg else 0}.p{pending}.t"
            + "-".join(str(counts[s]) for s in TASK_STATUSES) + f".c{capability_hash(db.company_settings(company))}")


def stamp_record(company: dict) -> dict:
    """companies.last_cycle_stamp parsed: {"s": stamp, "run": absorbed cycle run id}. A plain old value is
    read as the stamp itself."""
    raw = (company.get("last_cycle_stamp") or "").strip()
    if raw.startswith("{"):
        try:
            rec = json.loads(raw)
            if isinstance(rec, dict):
                return rec
        except ValueError:
            pass
    return {"s": raw}


def _save_record(company_id: int, rec: dict) -> str:
    raw = json.dumps(rec, sort_keys=True)
    db.update("companies", company_id, last_cycle_stamp=raw)
    return raw


def _absorb_last_cycle(company: dict) -> dict:
    """Once per finished planning cycle: when nobody but the CEO changed anything meaningful while it ran, its
    own effects (tasks it created or cancelled, the question it asked) become the new baseline, so a cycle never
    triggers the next one by itself. A cycle that failed (error, interrupted, stopped, out of steps) is no
    baseline: the stored stamp is cleared so the next idle or due trigger plans again. Returns the company with
    the record it now has."""
    cid = company["id"]
    run = db.one("SELECT id, agent_id, status, started_at FROM runs WHERE company_id=? AND kind='cycle' "
                 "ORDER BY id DESC LIMIT 1", (cid,))
    if not run or run["status"] == "running":
        return company
    rec = stamp_record(company)
    if rec.get("run") == run["id"]:
        return company
    since = run["started_at"] or ""
    outside = db.one(f"SELECT id FROM events WHERE company_id=? AND created_at>=? AND {REAL_CHANGE_SQL} AND NOT "
                     f"(kind IN ('fact','facts') AND agent_id IS ?) LIMIT 1", (cid, since, run["agent_id"]))
    owner = db.one("SELECT id FROM messages WHERE company_id=? AND sender='owner' AND created_at>=? LIMIT 1",
                   (cid, since))
    if run["status"] in FAILED_CYCLE:
        rec["s"] = ""
    elif not outside and not owner:
        rec["s"] = change_stamp(company)
    rec["run"] = run["id"]
    return {**company, "last_cycle_stamp": _save_record(cid, rec)}


def _work_open(company_id: int) -> bool:
    """Is any task queued or being worked on?"""
    return bool(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN ('todo','in_progress')",
                          (company_id,)))


def _waits_on_owner(company_id: int) -> bool:
    """Does anything wait on the owner: a pending question or request, or a task waiting for an answer?"""
    return bool(db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pending'", (company_id,))
                or db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status='waiting'", (company_id,)))


def should_cycle(company: dict, due: bool, idle: bool, requested: bool) -> bool:
    """Scheduler: may the CEO plan now?

    An owner action (requested) always plans. Otherwise planning needs a change since the last cycle: the idle
    trigger (no task queued or running) and the due trigger (every cycle_minutes) both skip while the change
    stamp is unchanged. Each due interval that passes without a change doubles the next one (cycle_backoff, up
    to eight doublings); any change resets it. While everything open waits on the owner, the owner gets one
    "still waiting on you" line a day instead of a planning cycle every few minutes.

    Safety net: when nothing is open and nothing waits on the owner, the company would otherwise sit idle
    forever, so the due trigger still plans at cycle_minutes * 2**cycle_backoff. Each such cycle raises the
    backoff (a cycle that created nothing counts too), so they space out: 1, 2, 4, 8... hours.
    """
    if requested:
        return True
    try:
        company = _absorb_last_cycle(company)
        stamp = change_stamp(company)
        backoff = int(company.get("cycle_backoff") or 0)
        if stamp != stamp_record(company).get("s"):
            if backoff:
                db.update("companies", company["id"], cycle_backoff=0)
            return bool(due or idle)
        if due:
            minutes = float(db.company_settings(company).get("cycle_minutes") or 60)
            if db.minutes_since(company.get("last_cycle_at")) >= minutes * (2 ** backoff):
                if not _work_open(company["id"]) and not _waits_on_owner(company["id"]):
                    db.update("companies", company["id"], cycle_backoff=min(MAX_BACKOFF, backoff + 1))
                    return True
                if backoff < MAX_BACKOFF:
                    db.update("companies", company["id"], cycle_backoff=backoff + 1)
        _maybe_waiting_notice(company)
        return False
    except Exception:   # a broken stamp must not stop the company from planning at all
        traceback.print_exc()
        return bool(due or idle)


def next_due_minutes(company: dict) -> float:
    """Minutes until the due trigger looks again (0 when it is due now). For the dashboard."""
    minutes = float(db.company_settings(company).get("cycle_minutes") or 60)
    interval = minutes * (2 ** int(company.get("cycle_backoff") or 0))
    return max(0.0, interval - db.minutes_since(company.get("last_cycle_at")))


def cycle_started(company: dict, requested: bool = False) -> None:
    """A planning cycle is starting (the scheduler, or the owner's "Plan now"). Record the change stamp (the
    baseline for the next decision) and whether anything changed, for this cycle's header. A change or an owner
    request resets the backoff; a safety-net cycle (nothing changed, see should_cycle) keeps the backoff it
    raised, so the next one waits twice as long."""
    try:
        cid = company["id"]
        fresh = db.get_company(cid) or company
        rec = stamp_record(fresh)
        stamp = change_stamp(fresh)
        changed = stamp != rec.get("s")
        _cycle_flags[cid] = (time.time(), changed, bool(requested))
        new = {"s": stamp}
        if rec.get("run"):
            new["run"] = rec["run"]
        _save_record(cid, new)
        if changed or requested:
            db.update("companies", cid, cycle_backoff=0)
    except Exception:
        traceback.print_exc()


def changed_since_last_cycle(company: dict) -> bool:
    """Did anything meaningful change since the previous planning cycle? Right after cycle_started this is its
    answer; otherwise the live stamp is compared with the stored one."""
    flag = _cycle_flags.get(company["id"])
    if flag and time.time() - flag[0] < FLAG_SECONDS:
        return flag[1]
    fresh = db.get_company(company["id"]) or company
    return change_stamp(fresh) != stamp_record(fresh).get("s")


def _nothing_changed_line(company: dict, requested: bool) -> bool:
    """Should this cycle's header say "Nothing has changed"? Decided once, at the start of each cycle: the flag
    cycle_started left is used up here, so a later cycle never reuses an old answer. Never for a cycle the owner
    asked for ("Plan now", an answer, a settings change): the owner acting is news."""
    flag = _cycle_flags.pop(company["id"], None)
    if flag and time.time() - flag[0] >= FLAG_SECONDS:
        flag = None
    if requested or (flag and flag[2]):
        return False
    if flag:
        return not flag[1]
    fresh = db.get_company(company["id"]) or company
    return change_stamp(fresh) == stamp_record(fresh).get("s")


def waiting_summary(company: dict) -> dict | None:
    """When everything open waits on the owner: what waits and since when; None while work can still move."""
    cid = company["id"]
    counts = task_counts(cid)
    if counts["todo"] or counts["in_progress"]:
        return None
    pending = db.q("SELECT action, created_at FROM approvals WHERE company_id=? AND status='pending'", (cid,))
    tasks = db.q("SELECT updated_at FROM tasks WHERE company_id=? AND status IN ('waiting','blocked')", (cid,))
    if not pending and not tasks:
        return None
    questions = sum(1 for p in pending if p["action"] == "question")
    requests = len(pending) - questions
    parts = []
    if questions:
        parts.append(f"{questions} question{'s' if questions != 1 else ''}")
    if requests:
        parts.append(f"{requests} approval request{'s' if requests != 1 else ''}")
    if tasks:
        parts.append(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''} waiting or blocked")
    stamps = [p["created_at"] for p in pending if p["created_at"]] + [t["updated_at"] for t in tasks if t["updated_at"]]
    oldest = min(stamps) if stamps else None
    return {"questions": questions, "requests": requests, "tasks": len(tasks), "what": ", ".join(parts),
            "oldest": oldest, "oldest_age": _age(oldest) if oldest else ""}


def _maybe_waiting_notice(company: dict) -> None:
    """One "still waiting on you" line per company per day while nothing changes and everything waits."""
    cs = db.company_settings(company)
    today = _today()
    if cs.get("last_waiting_notice") == today:
        return
    info = waiting_summary(company)
    if not info:
        return
    update_company_settings(company["id"], last_waiting_notice=today)   # first, so a send problem never repeats it
    plain = (f"{company['name']} is still waiting on you: {info['what']}. The oldest has waited {info['oldest_age']}. "
             "Planning is paused until something changes.")
    bus.emit(company["id"], "info", plain, None, {"waiting_notice": True, **{k: info[k] for k in
                                                                             ("questions", "requests", "tasks")}})
    try:
        from . import notify   # imported here: notify imports modules that import this one
        notify.send_later(f"<b>{notify.escape(company['name'])}</b> is still waiting on you: "
                          f"{notify.escape(info['what'])}. The oldest has waited {info['oldest_age']}.", kind="brief")
    except Exception as e:   # the phone must never break the scheduler
        print(f"notify failed: {e}")


# =============================================================== 3.4 daily token budget

def _day_start_utc() -> str:
    """Local midnight today, as the UTC timestamp format runs use."""
    midnight = _dt.datetime.combine(_dt.date.today(), _dt.time()).astimezone()
    return midnight.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tokens_today(company_id: int) -> int:
    """Tokens of the company's runs started today (local day), owner chat excluded."""
    return int(db.scalar("SELECT COALESCE(SUM(tokens), 0) FROM runs WHERE company_id=? AND started_at>=? AND "
                         "kind!='chat'", (company_id, _day_start_utc())) or 0)


def daily_budget(company: dict) -> int:
    try:
        return max(0, int(float(db.company_settings(company).get("daily_token_budget") or 0)))
    except (TypeError, ValueError):
        return 0


def over_budget(company: dict) -> bool:
    """Scheduler: has the company used its daily token budget (company setting daily_token_budget, 0 = off)?
    The first time a company crosses it in a day, the feed and the owner's phone hear about it once."""
    budget = daily_budget(company)
    if budget <= 0:
        return False
    used = tokens_today(company["id"])
    if used <= budget:
        return False
    try:
        _budget_notice(company, used, budget)
    except Exception:
        traceback.print_exc()
    return True


def _budget_notice(company: dict, used: int, budget: int) -> None:
    today = _today()
    if db.company_settings(company).get("budget_notice") == today:
        return
    update_company_settings(company["id"], budget_notice=today)
    text = (f"{company['name']} used its daily token budget ({used:,} of {budget:,} tokens). Background work is "
            "paused until midnight; chat and tasks you create still run. Raise daily_token_budget in the company "
            "settings to continue today.")
    bus.emit(company["id"], "budget", text, None, {"used": used, "budget": budget})
    try:
        from . import notify
        notify.send_later(f"<b>{notify.escape(company['name'])}</b> used its daily token budget ({used:,} of "
                          f"{budget:,} tokens). Background work is paused until midnight; chat still works.",
                          kind="important")
    except Exception as e:
        print(f"notify failed: {e}")


def budget_status() -> list[dict]:
    """Every company that is not archived: [{company_id, name, used, budget, over}] for the Doctor and the
    dashboard. budget 0 means no limit."""
    out = []
    for c in db.q("SELECT * FROM companies WHERE status!='archived' ORDER BY id"):
        budget = daily_budget(c)
        used = tokens_today(c["id"])
        out.append({"company_id": c["id"], "name": c["name"], "used": used, "budget": budget,
                    "over": bool(budget and used > budget)})
    return out


# =============================================================== 4.2 one charter path, validated

# Section name -> pattern over a normalised heading. A heading can satisfy several sections ("Offer and
# business model"). Synonyms cover what local models write ("Offer & Pricing", "Go-to-Market", "Who it's for").
CHARTER_SECTIONS: list[tuple[str, re.Pattern]] = [
    ("One-liner", re.compile(r"\bone-?liner\b|\bone liner\b|\btagline\b|\belevator pitch\b")),
    ("Customer", re.compile(r"^(the )?(target |ideal )?customers?\b|\btarget (market|audience|customers?|users?)\b|"
                            r"\bideal customer\b|\bwho (it is|its|we serve|we are|were|this is) for\b|^audience\b|"
                            r"\bcustomer (segment|profile)s?\b")),
    ("Problem", re.compile(r"\bproblems?\b|\bpain points?\b")),
    ("Offer and price", re.compile(r"\boffer\b|\bpric(e|es|ing)\b")),
    ("How we reach customers", re.compile(r"\bhow we (reach|find|get)\b|\bgo-to-market\b|\bgo to market\b|\bgtm\b|"
                                          r"\bchannels?\b|\bdistribution\b|\bacquisition\b|\bmarketing plan\b|"
                                          r"\breach(ing)? customers\b")),
    ("Competitors", re.compile(r"\bcompetit\w*|\balternatives\b")),
    ("Business model", re.compile(r"\bbusiness model\b|\brevenue model\b|\bunit economics\b|\bmonetis\w*|"
                                  r"\bmonetiz\w*|\bwho pays\b|\bhow we make money\b")),
    ("Riskiest assumption", re.compile(r"\briskiest\b|\b(key|critical|biggest) assumption|\bassumption to test\b")),
    ("North Star metric", re.compile(r"\bnorth[- ]?star\b")),
    ("30-day plan", re.compile(r"\b30-? ?days?\b|\bfirst month\b|\bthirty-? ?days?\b|\bweeks? 1 ?(-|to) ?4\b|"
                               r"\b(first )?(four|4) weeks\b")),
]
SECTION_MIN_CHARS = 20    # a section with less real text than this counts as empty
_WHO_PAYS_RE = re.compile(r"\bpa(y|ys|id|ying)\b|charg|subscri|\bfees?\b|commission|revenue|pric|\bper\b|"
                          r"[$\u20ac\u00a3]")

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
# "**One-liner**: text", "- **One-liner:** text", "__Pricing__ - text"
_BOLD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)([^*_\n]{2,60}?)(?:\*\*|__)\s*[:\-]?\s*(.*)$")
_PLAIN_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?([A-Za-z0-9][\w &'/\-\u2010-\u2015]{1,40}):\s*(.*)$")
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_CURRENCY_RE = re.compile(r"[$\u20ac\u00a3\u00a5]\s?\d|\d+(?:[.,]\d+)?\s?(?:usd|eur|gbp|cad|aud|dollars?|euros?|"
                          r"pounds?)\b", re.I)
# A pass threshold written in words: "three out of ten roofers", "half of the visitors", "five percent".
_WORD_THRESHOLD_RE = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twenty|half)\s+"
                                r"(out\s+of|of|in|percent)\b")


def _norm_heading(text: str) -> str:
    s = (text or "").translate(_DASHES).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[*_`:\"\u2018\u2019']", "", s)
    s = re.sub(r"^\s*(?:\d+[.)]|[a-z][.)])\s+", "", s)
    return " ".join(s.split())


def _section_names(heading: str) -> list[str]:
    h = _norm_heading(heading)
    return [name for name, rx in CHARTER_SECTIONS if rx.search(h)]


def _plain(text: str) -> str:
    """The readable text of a block: list markers, emphasis, table pipes and link syntax removed."""
    out = []
    for line in (text or "").splitlines():
        s = re.sub(r"^\s*(?:[-*+]|\d+[.)]|>)\s+", "", line)
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"[*_`|]+", " ", s)
        s = re.sub(r"^[\s:\-]+$", "", s)
        out.append(" ".join(s.split()))
    return " ".join(x for x in out if x)


def charter_blocks(text: str) -> list[tuple[str, int, str]]:
    """Split a charter into (heading, level, body) blocks. Markdown headings run until the next heading of the
    same or a higher level; bold or plain labels ("**Pricing**: $20 per month") are blocks of their own that run
    until the next heading or label."""
    lines = (text or "").splitlines()
    marks: list[tuple[int, str, int, str]] = []   # (line index, heading, level, text on the same line)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            marks.append((i, m.group(2), len(m.group(1)), ""))
            continue
        m = _BOLD_LABEL_RE.match(line)
        if m and _section_names(m.group(1)):
            marks.append((i, m.group(1), 7, m.group(2)))
            continue
        m = _PLAIN_LABEL_RE.match(line)
        if m and _section_names(m.group(1)):
            marks.append((i, m.group(1), 7, m.group(2)))
    blocks = []
    for n, (i, heading, level, same_line) in enumerate(marks):
        end = len(lines)
        for j, _h, lvl, _s in marks[n + 1:]:
            if lvl <= level or level == 7:
                end = j
                break
        body = "\n".join([same_line] + lines[i + 1:end]) if same_line else "\n".join(lines[i + 1:end])
        blocks.append((heading, level, body))
    return blocks


def _list_items(body: str) -> int:
    """Named items in a block: bullet or numbered lines, plus table rows after the header row."""
    bullets = sum(1 for ln in body.splitlines()
                  if re.match(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+\S", ln) and len(_plain(ln)) >= 2)
    rows = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
    data_rows = [r for r in rows if not re.fullmatch(r"[\s|:\-]+", r.strip())]
    table = max(0, len(data_rows) - 1) if data_rows else 0
    return max(bullets, table)


def _section_problem(name: str, body: str) -> str | None:
    """Why a section's content does not count, or None when it does."""
    plain = _plain(body)
    if len(plain) < SECTION_MIN_CHARS:
        return "empty"
    low = plain.lower()
    if name == "Offer and price":
        has_number = bool(re.search(r"\d", low))
        if not (_CURRENCY_RE.search(plain) or (has_number and re.search(r"price|\bper\b|/\s?mo", low))
                or "to test" in low):
            return "needs a price, or a price range to test"
    elif name == "Competitors":
        if _list_items(body) < 3:
            return "name at least three, one per bullet or table row, with how we differ"
    elif name == "Riskiest assumption":
        if not re.search(r"\d", low) and not _WORD_THRESHOLD_RE.search(low):
            return "needs a pass threshold: a number or percentage"
    elif name == "Business model":
        if not _WHO_PAYS_RE.search(low):
            return "say who pays and for what"
    return None


def validate_charter(text: str) -> list[str]:
    """Required charter sections that are missing, empty or lack what they must say. Each entry is the section
    name, followed by the reason in brackets when the section exists but does not count yet."""
    found: dict[str, list[str]] = {}
    for heading, _level, body in charter_blocks(text):
        for name in _section_names(heading):
            found.setdefault(name, []).append(body)
    missing = []
    for name, _rx in CHARTER_SECTIONS:
        bodies = found.get(name)
        if not bodies:
            missing.append(name)
            continue
        problems = [_section_problem(name, b) for b in bodies]
        if all(problems):
            reason = next((p for p in problems if p != "empty"), "empty")
            missing.append(name if reason == "empty" else f"{name} ({reason})")
    return missing


def missing_names(missing: list[str]) -> list[str]:
    """Section names only, without the reasons."""
    return [m.split(" (", 1)[0] for m in missing]


CHARTER_PATH = "docs/charter.md"
CRITIQUE_PATH = "docs/critique.md"


def _rel(path: str) -> str:
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def charter_write_hook(ctx: Ctx, rel_path: str, content: str) -> str | None:
    """write_file/edit_file on docs/charter.md goes through the same validation as update_charter and saves to
    the company record, so there is one charter path whatever tool the model picks. Returns the refusal text
    (nothing is written) or None (the file is written). The critic stage of founding may only write
    docs/critique.md."""
    rel = _rel(rel_path)
    if getattr(ctx, "founding_stage", "") == "critic" and rel.lower() != CRITIQUE_PATH:
        return f"ERROR: in this review you can only write {CRITIQUE_PATH}. Put your critique there."
    if rel.lower() != CHARTER_PATH:
        return None
    missing = validate_charter(content)
    if missing:
        return ("ERROR: the charter is missing these sections, or they are empty: " + "; ".join(missing)
                + ". Add them and write it again. Give each one its own heading (for example '## Competitors'); "
                  "an estimate labelled as an estimate is fine.")
    save_charter(ctx, content)
    return None


def save_charter(ctx: Ctx, text: str) -> None:
    """Store a validated charter written as a file the way update_charter does: the charter column, the
    one-liner as the mission (when empty, or while founding writes the charter) and the stage idea -> building."""
    from . import agents   # imported here: agents imports this module
    cid = ctx.company["id"]
    company = db.get_company(cid) or ctx.company
    fields: dict = {"charter": (text or "")[:200_000]}
    stage = db.company_settings(company).get("founding_stage")
    if not (company.get("mission") or "").strip() or stage == "charter":
        mission = agents.mission_from_charter(text)
        if mission:
            fields["mission"] = mission
    if (company.get("stage") or "idea") == "idea":
        fields["stage"] = "building"
    db.update("companies", cid, **fields)
    ctx.company.update(fields)
    bus.emit(cid, "charter", f"saved {CHARTER_PATH} as the company charter", ctx.agent.get("id"))
    bus.notify("company", company_id=cid)


# =============================================================== 3.2, 4.5 the planning-cycle header

PLAN_RULES = [
    "The Facts, Since your last session and Waiting on the owner sections of your instructions are the real "
    "state of the company. Read them first, and never ask the owner something that is already waiting there.",
    "If nothing changed and the open work is waiting on the owner, finish at once with status 'waiting' and a "
    "one-line note. Plan only work that does not need the answer.",
    "Prefer work that touches real customers (conversations, signups, replies) over internal documents. Never "
    "create a task whose only output is a status file.",
    "Every new task names the goal it serves and what it should change.",
]


def cycle_header(company: dict, requested: bool = False) -> str:
    """Top of the planning-cycle prompt: whether anything changed, the playbook's next experiments and the
    planning rules. The facts, changes and waiting items themselves are in the system prompt (truth.py).
    requested: the owner asked for this cycle, so it never opens with "Nothing has changed"."""
    parts = []
    if _nothing_changed_line(company, requested):
        parts.append("Nothing has changed since your last planning cycle.")
    key = playbooks.archetype(company)
    if key:
        exp = playbooks.PLAYBOOKS[key]["experiments"][:3]
        parts.append(f"## Next experiments ({playbooks.PLAYBOOKS[key]['label']} playbook)\n"
                     + "\n".join(f"- {e}" for e in exp))
    parts.append("## How to plan this cycle\n" + "\n".join(f"- {r}" for r in PLAN_RULES))
    return "\n\n".join(parts)


# =============================================================== 4.1, 4.4 staged founding

FOUNDING_STAGES = ("research", "charter", "critic", "plan")
STAGE_STEPS = {"research": 12, "charter": 10, "critic": 6, "plan": 12}
STAGE_TEXT = {"research": "researching the market", "charter": "writing the charter",
              "critic": "a skeptical investor is reviewing the charter", "plan": "planning the first week"}
STAGE_THINK = {"research": "medium", "charter": "high", "critic": "high", "plan": "high"}


def founding_stage(company: dict) -> str:
    """The stage a founding run should do now: research, charter, critic or plan; "done" once founding
    finished. A company without the setting that is still being founded (created before staged founding)
    starts at research; one without the setting that is not founding was founded before Phase 0.5, so its
    founding is done and "Retry founding" restarts where it is weakest (retry_stage), not at the beginning."""
    st = str(db.company_settings(company).get("founding_stage") or "").strip().lower()
    if st in FOUNDING_STAGES or st == "done":
        return st
    return "research" if (company.get("status") or "founding") == "founding" else "done"


def retry_stage(company: dict) -> str:
    """Where "Retry founding" restarts once founding has finished: research when there is no research file, the
    charter when it does not pass validation, otherwise the plan."""
    try:
        if not workspace.safe_path(company, "research/market.md").is_file():
            return "research"
    except Exception:
        return "research"
    if validate_charter(company.get("charter") or ""):
        return "charter"
    return "plan"


def set_founding_stage(company_id: int, stage: str, **extra) -> None:
    update_company_settings(company_id, founding_stage=stage, **extra)


RESEARCH_ENDINGS = {"partial": "early (partial)", "out_of_steps": "at its step budget", "blocked": "blocked",
                    "waiting": "waiting on the owner"}


def _summary_line(summary: str) -> str:
    """The "Summary:" line of a finish handoff, or the text itself, on one line."""
    text = summary or ""
    for line in text.splitlines():
        if line.startswith("Summary:"):
            text = line[len("Summary:"):]
            break
    return " ".join(text.split())[:300] or "no summary"


def after_stage(company: dict, stage: str, how: str, summary: str = "") -> str:
    """A founding stage ended (done, partial or out of steps): decide what comes next and record it. Returns the
    stage to start next, or "done" when founding is complete.

    The charter stage only moves on with a charter in the record that passes validation. It gets one retry with
    the missing sections in its prompt; after that the owner gets the founding question (Phase 0's
    founding_partial pattern) and founding carries on with the critic and the plan, since a plan built on a thin
    charter is still more useful than no tasks at all.
    """
    cid = company["id"]
    if stage == "research":
        # Research may end early (search down, step budget): the charter stage is told, so it notes the gaps
        # instead of filling them with invented numbers.
        note = "" if how == "done" else f"Research ended {RESEARCH_ENDINGS.get(how, how)}: {_summary_line(summary)}"
        if not note and not _research_file(company)[0]:
            note = f"Research ended without saving {RESEARCH_PATH}: {_summary_line(summary)}"
        update_company_settings(cid, founding_research_note=note[:500])
    if stage == "charter":
        missing = validate_charter(company.get("charter") or "")
        if missing:
            names = missing_names(missing)
            if db.company_settings(company).get("founding_retry") != "charter":
                update_company_settings(cid, founding_retry="charter")
                bus.emit(cid, "founding", "Founding: the charter is not complete yet (missing "
                         + ", ".join(names) + "), so the CEO writes it once more")
                return "charter"
            update_company_settings(cid, founding_retry="")
            context = ("The charter stage ran twice and the charter still lacks: " + "; ".join(missing)
                       + ". Founding continues with the critique and the first-week plan. Continue anyway keeps "
                         "the charter as it is (you can edit it on the company page); Retry founding writes it "
                         "again once founding has finished.")
            try:
                from . import watchdog   # imported here: watchdog imports tools, which imports this module
                watchdog.founding_ended(db.get_company(cid) or company,
                                        "the charter still lacks " + ", ".join(names), context)
            except NotImplementedError:
                pass
            except Exception:
                traceback.print_exc()
    i = FOUNDING_STAGES.index(stage) if stage in FOUNDING_STAGES else len(FOUNDING_STAGES) - 1
    nxt = FOUNDING_STAGES[i + 1] if i + 1 < len(FOUNDING_STAGES) else "done"
    set_founding_stage(cid, nxt, founding_retry="")
    return nxt


def _tool_ready(name: str) -> bool:
    return name in REGISTRY


def founding_prompt(company: dict, context: str, stage: str, roles_list: str, named: bool,
                    search_down: bool) -> str:
    """The user prompt of one founding stage. Each starts with "You are founding a brand-new company. Stage:
    <stage>" so logs, tests and the fake model can tell the stages apart."""
    n = FOUNDING_STAGES.index(stage) + 1 if stage in FOUNDING_STAGES else 1
    head = (f"You are founding a brand-new company. Stage: {stage} ({n} of 4: research, charter, critic, plan).\n"
            "The owner's idea:\n"
            f'"""{company.get("idea")}"""\n'
            + (f"\nAbout the owner and their constraints:\n{context}\n" if context else "")
            + (f"\nThe owner already chose the name: {company['name']}. Keep it.\n" if named else "")
            + f"\nYour team's roles: {roles_list}.\n")
    steps = STAGE_STEPS.get(stage, 12)
    if stage == "charter":
        return head + _charter_text(company, named, steps)
    if stage == "critic":
        return head + _critic_text(steps)
    if stage == "plan":
        return head + _plan_text(steps)
    return head + _research_text(steps, search_down)


def _research_text(steps: int, search_down: bool) -> str:
    note = (
        "\nWeb search is down right now. Do not call web_search more than once. Use fetch_url on addresses you "
        "already know, write research/market.md from the idea and what you know, mark every gap, and finish with "
        "status 'partial'. The plan stage asks the owner about the market in the welcome brief (report_to_owner).\n"
    ) if search_down else ""
    fact = ("3. Record prices and other key numbers with record_fact as you find them (the claim, the URL and a "
            "quote copied exactly from the page you fetched).\n"
            if _tool_ready("record_fact") else
            "3. For every price or number you will rely on, open the page with fetch_url so you can cite it.\n")
    return (
        f"\nThis stage: research the market. You have {steps} tool calls: save your findings and finish by call "
        f"{max(2, steps - 2)}.\n" + note
        + "\nDo this, in order:\n"
        "1. Research the market with web_search and fetch_url (3 to 6 searches): who the customers are, what they "
        "pay for this today and to whom, at least three real competitors with their prices, and where these "
        "customers can be reached (communities, directories, search terms, events).\n"
        f"2. Write {RESEARCH_PATH} early, as soon as you have the first findings, and append to it as you go "
        "(write_file with append=true): a source URL or [fact N] next to every price and number, and a Gaps list "
        "for what you could not find. Pages you only read are lost when this stage ends; what you save is what "
        "the next stages know.\n"
        + fact
        + "4. Call finish. Status 'partial' is fine when the research is incomplete: the next stages work with what "
        "you saved. Do not write the charter or create tasks in this stage."
    )


# What each founding stage must leave behind (design 4.1). Founding runs have no task, so tools.finish asks
# planning for this instead of checking a task deliverable. The first real gpt-oss founding spent its research
# steps fetching pages, never wrote research/market.md, and still claimed it had.
RESEARCH_PATH = "research/market.md"
RESEARCH_MIN_CHARS = 200
PLAN_MIN_TASKS = 3
RESEARCH_CHECKPOINT = (f"Save what you found so far to {RESEARCH_PATH} now, each fact with the URL it came from, "
                       "then continue.")


def _research_file(company: dict) -> tuple[bool, int]:
    """(exists, characters of real text) of research/market.md."""
    try:
        p = workspace.safe_path(company, RESEARCH_PATH)
        if not p.is_file():
            return False, 0
        return True, len(p.read_text(encoding="utf-8", errors="replace").strip())
    except Exception:
        return False, 0


def research_saved(company: dict) -> bool:
    """Has the research stage saved research/market.md yet? (agents.run's halfway checkpoint)"""
    return _research_file(company)[0]


def fetched_urls(company_id: int, limit: int = 12) -> list[str]:
    """The pages this company fetched, newest first, for the research stage to write down."""
    rows = db.q("SELECT url, MAX(id) AS last FROM fetched WHERE company_id=? GROUP BY url ORDER BY last DESC LIMIT ?",
                (company_id, limit))
    return [r["url"] for r in rows if r["url"]]


def stage_problem(company: dict, stage: str) -> str | None:
    """What a founding stage has not left behind yet, as the reason tools.finish gives once ("Not finished yet:
    ..."); None when the stage's deliverable is there. research: research/market.md with real content; critic:
    docs/critique.md; plan: at least three tasks; charter: a charter in the record that passes validation."""
    company = db.get_company(company["id"]) or company
    if stage == "research":
        exists, size = _research_file(company)
        if size >= RESEARCH_MIN_CHARS:
            return None
        what = (f"{RESEARCH_PATH} has only {size} characters" if exists else f"{RESEARCH_PATH} does not exist")
        text = (f"{what}. Write what you found there now: each fact with the URL it came from, and a Gaps list for "
                "what you could not find. Then call finish again.")
        urls = fetched_urls(company["id"])
        if urls:
            text += " Pages this company fetched: " + ", ".join(urls) + "."
        return text
    if stage == "critic":
        try:
            p = workspace.safe_path(company, CRITIQUE_PATH)
            ok = p.is_file() and bool(p.read_text(encoding="utf-8", errors="replace").strip())
        except Exception:
            ok = False
        if ok:
            return None
        return (f"{CRITIQUE_PATH} does not exist. Write it with write_file: the three strongest reasons this company "
                "fails, the evidence that would answer each, and what to test first. Then call finish again.")
    if stage == "plan":
        n = int(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status!='cancelled'",
                          (company["id"],)) or 0)
        if n >= PLAN_MIN_TASKS:
            return None
        return (f"the first week has {n} task{'s' if n != 1 else ''}; it needs at least {PLAN_MIN_TASKS}. Create the "
                "rest with create_task across the team, each with a deliverable and acceptance criteria, then call "
                "finish again.")
    if stage == "charter":
        missing = validate_charter(company.get("charter") or "")
        if not missing:
            return None
        return ("the company record has no complete charter yet (missing or empty: " + "; ".join(missing)
                + "). Call update_charter with the full charter, one '## ' heading per section, then call finish "
                  "again.")
    return None


CHARTER_GUIDE = [
    ("One-liner", "what we do and for whom, in one sentence."),
    ("Customer", "who exactly buys, and who uses it if that is someone else."),
    ("Problem", "what hurts today and what they do about it now."),
    ("Offer and price", "what we sell and a price, or a price range to test."),
    ("How we reach customers", "the channels, and where the first 20 customers come from."),
    ("Competitors", "at least three real ones, one per bullet or table row, with their price and how we differ."),
    ("Business model", "who pays, for what, and a unit-economics estimate labelled as an estimate."),
    ("Riskiest assumption", "the belief that sinks the company if it is wrong, with a pass threshold, for "
                            "example \"At least 3 of 10 providers say they would pay $40 per lead\"."),
    ("North Star metric", "the one number that shows customers get value."),
    ("30-day plan", "week by week, starting with contact with real customers."),
]


def _charter_text(company: dict, named: bool, steps: int) -> str:
    saved = (company.get("charter") or "").strip()
    missing = validate_charter(saved) if saved else [n for n, _ in CHARTER_GUIDE]
    retry = str(db.company_settings(company).get("founding_retry") or "") == "charter"
    lines = [f"\nThis stage: write the charter. You have {steps} tool calls."]
    if retry:
        lines.append("Your last attempt did not save a complete charter. Missing or empty: " + "; ".join(missing)
                     + ". Fix exactly these and call update_charter again.")
    elif saved and not missing:
        lines.append("A complete charter is already saved (see The company section). Check it against the list "
                     "below; call update_charter only to fix something thin or wrong, then finish.")
    elif saved:
        lines.append("A draft charter is saved but incomplete. Missing or empty: " + "; ".join(missing) + ".")
    research_note = str(db.company_settings(company).get("founding_research_note") or "").strip()
    if research_note:
        lines.append(research_note.rstrip(".") + ". The research has gaps: name them in the charter, label "
                     "estimates as estimates and never invent numbers to fill them.")
    lines.append("Read research/market.md first. Where research has gaps, label estimates as estimates and keep "
                 "the gaps visible.")
    lines.append("Call update_charter with the full charter in Markdown, one '## ' heading per section. It is "
                 "refused until every section below has real content:")
    lines += [f"- {name}: {what}" for name, what in CHARTER_GUIDE]
    pb = playbooks.section(company)
    if pb:
        lines.append("Use this playbook for the type of business: build its riskiest assumption and first "
                     "experiments into the charter.\n" + pb)
    lines.append(("" if named else "Give the company a short, memorable name with company_name. ")
                 + "Pass one_liner and stage 'building'. Then call finish.")
    return "\n".join(lines)


def _critic_text(steps: int) -> str:
    return (
        f"\nThis stage: an outside review. For this run you are a skeptical investor, not the founder: your job is "
        f"to find why this company fails. You have {steps} tool calls.\n"
        "1. Read docs/charter.md and research/market.md.\n"
        f"2. Write {CRITIQUE_PATH} with two sections:\n"
        "## Three strongest reasons this fails\n"
        "For each: the reason, why it is likely for this business (two-sided demand, price, reaching customers, "
        "competitors, rules), and the evidence that would answer it (a test with a pass threshold, a number to "
        "find, a source).\n"
        "## What to test first\n"
        "The cheapest test that would change our mind, in one line.\n"
        f"3. Call finish. You may check one claim with web_search or fetch_url; you can only write {CRITIQUE_PATH}."
    )


def _plan_text(steps: int) -> str:
    return (
        f"\nThis stage: plan the first week. You have {steps} tool calls.\n"
        "Do this, in order:\n"
        f"1. Read docs/charter.md and {CRITIQUE_PATH}.\n"
        "2. Create 5 to 8 first tasks with create_task across the team (assign by role), each with a deliverable "
        "(a file path, or an action like 'email queued') and 3 to 5 acceptance criteria. Test demand before "
        "building product: the first week must include contact with real customers or providers. The first "
        "engineering task is a landing page at site/index.html with a clear offer and a working signup form. "
        "Never create a task whose only output is a status file.\n"
        f"3. Turn each risk in {CRITIQUE_PATH} into a task, or into an acceptance criterion of one, or name it in "
        "the welcome brief as a risk we accept and why.\n"
        "4. Send the owner a short welcome brief with report_to_owner: the plan for week one, the riskiest "
        "assumption and how we will test it, and any research gaps they could fill.\n"
        "5. If the company needs something only the owner can give (a mailbox, a postal address for email, a "
        "domain, a budget), ask one consolidated question with ask_owner: the options one per line and your "
        "recommendation. Ask nothing else.\n"
        "6. Call finish."
    )


# =============================================================== dashboard

def planning_status(company: dict) -> dict:
    """What the dashboard shows about planning effort for one company."""
    rec = stamp_record(company)
    stamp = change_stamp(company)
    return {"company_id": company["id"], "changed": stamp != rec.get("s"), "stamp": stamp,
            "last_cycle_stamp": rec.get("s", ""), "cycle_backoff": int(company.get("cycle_backoff") or 0),
            "next_due_minutes": round(next_due_minutes(company), 1), "waiting": waiting_summary(company),
            "founding_stage": founding_stage(company) if company.get("status") == "founding" else
            str(db.company_settings(company).get("founding_stage") or ""),
            "archetype": playbooks.archetype(company),
            "budget": {"used": tokens_today(company["id"]), "budget": daily_budget(company),
                       "over": bool(daily_budget(company) and tokens_today(company["id"]) > daily_budget(company))}}


@route("GET", r"/api/companies/(\d+)/planning")
def api_planning(h, m, body):
    return {"planning": planning_status(company_or_404(m.group(1)))}


@route("GET", r"/api/budget")
def api_budget(h, m, body):
    return {"budget": budget_status()}
