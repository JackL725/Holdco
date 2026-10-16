"""Phone notifications (Phase 0): a Telegram bot with inline buttons, and ntfy as a notify-only option.

Long polling (getUpdates) means nothing on this PC is exposed to the internet. Everything here is best effort:
a phone that cannot be reached must never block an agent, so send() prints the problem and returns False
instead of raising, and the hooks (approval_requested, error, set_kill_switch) hand their HTTP sends to one
background sender thread and return at once; flush() waits for that thread (tests and shutdown).

The bot token is never printed: scrub() replaces the token, and any bot URL that carries it, with "[token]"
in every error text before it reaches the console, an event, a Doctor row or an API reply.
"""
from __future__ import annotations

import hashlib
import html
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import bus, db
from .routing import route

# Tunables. Module constants rather than settings: the owner has no reason to change them.
REPEAT_SECONDS = 3600          # identical text is not resent within this window
ERROR_REPEAT_SECONDS = 3600    # one message per distinct (normalised) error message per hour
POLL_TIMEOUT = 25              # seconds one getUpdates call waits for activity (Telegram's long poll)
BACKOFF_MIN = 2.0              # seconds to wait after a poller error; doubles up to BACKOFF_MAX
BACKOFF_MAX = 60.0
HTTP_TIMEOUT = 20              # seconds for an ordinary Bot API or ntfy call
MAX_TEXT = 4000                # Telegram caps a message at 4096 characters
IMPORTANT_KINDS = ("important", "request", "escalation", "error", "brief")   # what notify_level "important" sends
CHECK = "✓ "              # prefix on the recommended option's button
TOKEN_MASK = "[token]"    # what scrub() puts where the bot token (or its URL) was
HELP_TEXT = "Commands: /status, /stop, /resume. Answer requests with the buttons."
TEST_TEXT = "Holdco test: notifications work."
KILL_ON_TEXT = "Kill switch ON. Agents stopped, outbound actions frozen. Send /resume to turn it off."
KILL_OFF_TEXT = "Kill switch off. Agents may work again."

# Bot API method names in one place, so a correction is a one-line change.
METHODS = {
    "get_me": "getMe",
    "get_updates": "getUpdates",
    "send": "sendMessage",
    "answer_callback": "answerCallbackQuery",
    "edit_markup": "editMessageReplyMarkup",
    "edit_text": "editMessageText",
}

# Any Bot API URL, with or without its host: the token sits right after "/bot" and ends at the next slash.
# This is the backstop for a token we cannot match literally (urllib quotes a bad URL with repr(), so a
# newline pasted into the token shows up as the two characters "\n"; and some error paths run before the
# settings can be read).
_BOT_URL_RE = re.compile(r"(?:https?://)?[\w.\-]*(?::\d+)?/bot[^/\s'\"<>]+")

# In-memory state. Lost on restart, which is fine: it only throttles repeats and keeps the poller handle.
_recent: dict[str, float] = {}       # sha1(text) -> epoch of the last send
_errors: dict[str, float] = {}       # normalised error message -> epoch of the last notification
_sent_html: dict[str, str] = {}      # "chat:message_id" -> the HTML we sent, so an edit keeps the formatting
_failures: dict = {"count": 0, "last_error": "", "last_at": None}   # for the Doctor's telegram row
_lock = threading.Lock()
_poller_lock = threading.Lock()
POLLER: "Poller | None" = None
# Local fakes in tests must never go through a corporate proxy; real Telegram and ntfy use the system proxy.
_proxyless = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# The sender thread: hooks queue (callable, args, kwargs) here and return; one thread keeps the order.
_queue: queue.Queue = queue.Queue()
_worker: "threading.Thread | None" = None
_worker_lock = threading.Lock()


class NotifyError(Exception):
    """A channel rejected or could not receive a message (token already scrubbed from the text)."""


def configured(s: dict | None = None) -> bool:
    s = s or db.get_settings()
    return bool((s.get("telegram_bot_token") and _chat_id(s)) or s.get("ntfy_topic"))


# ---------------- small helpers ----------------

def _chat_id(s: dict) -> str:
    return str(s.get("telegram_chat_id") or "").strip()


def escape(text) -> str:
    """Escape dynamic text for Telegram's HTML parse mode. Only <, > and & matter there."""
    return html.escape("" if text is None else str(text), quote=False)


def _plain(text: str) -> str:
    """Turn the HTML we build for Telegram into plain text for ntfy."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def scrub(text, s: dict | None = None) -> str:
    """Replace the bot token, and the whole bot URL that carries it, with "[token]" in any text.

    Apply it to every error that may quote the request URL (urllib's URL errors, HTTP errors, the ValueError
    for a malformed api base) before the text reaches a Doctor row, an API reply, an event or the console.
    `s` is the settings dict when the caller has one; otherwise they are read from the database. Never raises:
    with no settings at hand the URL pattern alone still hides whatever follows "/bot".
    """
    text = "" if text is None else str(text)
    if s is None:
        try:
            s = db.get_settings()
        except Exception:
            s = {}
    token = str(s.get("telegram_bot_token") or "")
    if token.strip():
        # Literal, stripped and repr-escaped forms: a token saved with a stray newline appears as "\n" inside
        # the repr-quoted URL of urllib's error, where a plain replace() of the raw token would miss it.
        forms = {token, token.strip(), repr(token)[1:-1], repr(token.strip())[1:-1]}
        for form in sorted(forms, key=len, reverse=True):
            if form:
                text = text.replace(form, TOKEN_MASK)
    return _BOT_URL_RE.sub(TOKEN_MASK, text)


_scrub = scrub   # the old private name, kept for anything that imported it


def _key(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _record_failure(detail: str, s: dict | None = None) -> None:
    """Remember the last problem for the Doctor and print it. Everything passes through scrub() here, so no
    caller can leak the token to the console by mistake."""
    detail = scrub(detail, s)[:300]
    _failures["count"] += 1
    _failures["last_error"] = detail
    _failures["last_at"] = time.time()
    print(f"notify: {detail}")


def _wanted(kind: str, s: dict) -> bool:
    """notify_level "all" sends everything; "important" drops info-level chatter (run summaries and the like)."""
    if (s.get("notify_level") or "important") == "all":
        return True
    return kind in IMPORTANT_KINDS


def normalise(message: str) -> str:
    """Collapse numbers, URLs and spacing so repeats of the same problem count as one error."""
    m = re.sub(r"https?://\S+", "<url>", str(message or ""))
    m = re.sub(r"\d+", "#", m)
    return re.sub(r"\s+", " ", m).strip().lower()[:200]


def _open(req: urllib.request.Request, timeout: float):
    host = urllib.parse.urlparse(req.full_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _proxyless.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------- the sender thread ----------------

def _enqueue(fn, *args, **kwargs) -> None:
    """Run fn(*args, **kwargs) on the sender thread and return at once.

    The hooks use this so an agent, the scheduler or a dashboard button never waits on Telegram: with the
    phone network down an HTTP call can hold the caller for the whole HTTP_TIMEOUT. One thread, not one per
    message, so confirmations arrive in the order they were raised (a /stop then a /resume stay in order).
    """
    global _worker
    with _worker_lock:
        _queue.put((fn, args, kwargs))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, daemon=True, name="notify-sender")
            _worker.start()


def _drain() -> None:
    while True:
        fn, args, kwargs = _queue.get()
        try:
            fn(*args, **kwargs)
        except Exception as e:   # send() swallows its own errors; this keeps the thread alive for anything else
            _record_failure(f"background send failed: {e}")
        finally:
            _queue.task_done()


def send_later(text: str, buttons: list[list[dict]] | None = None, kind: str = "info", *, dedupe: bool = True) -> None:
    """Queue a message for the sender thread and return at once. Use this from agent threads and the scheduler;
    messages queued this way keep their order with request and question announcements."""
    _enqueue(send, text, buttons, kind, dedupe=dedupe)


def flush(timeout: float = 5.0) -> bool:
    """Wait until the sender thread has delivered everything queued so far (tests and shutdown).
    Returns False when the timeout passed first."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


# ---------------- channels ----------------

def _telegram(s: dict, method: str, body: dict, timeout: float = HTTP_TIMEOUT):
    """POST one Bot API call and return its "result". Raises NotifyError (token scrubbed) on any failure.

    The request is built inside the try on purpose: a malformed api base (no scheme, a stray newline in the
    token) makes urllib raise a ValueError that quotes the whole URL, token included.
    """
    token = s.get("telegram_bot_token") or ""
    if not token:
        raise NotifyError("Telegram: no bot token is set")
    base = (s.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/bot{token}/{method}", data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "User-Agent": "Holdco"})
        with _open(req, timeout) as r:
            raw = r.read(1_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read(20000).decode("utf-8", "replace")
        try:
            desc = json.loads(raw).get("description") or raw[:200]
        except (ValueError, AttributeError):
            desc = raw[:200]
        raise NotifyError(scrub(f"Telegram {method}: HTTP {e.code} {desc}", s)) from None
    except Exception as e:   # connection refused, DNS, timeout, reset, a bad URL: all look the same to the owner
        raise NotifyError(scrub(f"Telegram {method}: {getattr(e, 'reason', None) or e}", s)) from None
    try:
        out = json.loads(raw)
    except ValueError:
        raise NotifyError(f"Telegram {method}: unreadable reply") from None
    if not isinstance(out, dict) or not out.get("ok"):
        desc = out.get("description") if isinstance(out, dict) else ""
        raise NotifyError(scrub(f"Telegram {method}: {desc or 'request failed'}", s))
    return out.get("result")


def _keyboard(buttons: list[list[dict]] | None) -> dict | None:
    """Rows of {"text", "data"} -> Telegram inline keyboard. callback_data is capped at 64 bytes by Telegram."""
    rows = []
    for row in buttons or []:
        cells = [{"text": str(b.get("text") or "?")[:60], "callback_data": str(b.get("data") or "noop")[:64]}
                 for b in (row or []) if isinstance(b, dict)]
        if cells:
            rows.append(cells)
    return {"inline_keyboard": rows} if rows else None


def _remember_html(chat, message_id, text: str) -> None:
    _sent_html[f"{chat}:{message_id}"] = text
    while len(_sent_html) > 300:
        _sent_html.pop(next(iter(_sent_html)))


def _original_html(chat, message_id, plain_fallback: str) -> str:
    """The HTML of a message we sent earlier; Telegram hands callbacks the plain text, which we escape instead."""
    return _sent_html.get(f"{chat}:{message_id}") or escape(plain_fallback)


def _send_telegram(s: dict, text: str, buttons: list[list[dict]] | None = None) -> dict:
    body = {"chat_id": _chat_id(s), "text": text[:MAX_TEXT], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    kb = _keyboard(buttons)
    if kb:
        body["reply_markup"] = kb
    try:
        result = _telegram(s, METHODS["send"], body)
    except NotifyError as e:
        if "parse" not in str(e).lower():
            raise
        # Our HTML did not survive (a caller passed stray tags): the message still matters, so send it plain.
        body.pop("parse_mode")
        body["text"] = _plain(text)[:MAX_TEXT]
        result = _telegram(s, METHODS["send"], body)
    if isinstance(result, dict) and result.get("message_id") is not None:
        _remember_html(_chat_id(s), result["message_id"], text)
    return result or {}


def _send_ntfy(s: dict, text: str, kind: str = "info") -> None:
    """One POST of the plain text to <ntfy_server>/<ntfy_topic>; the app shows "Holdco" as the title."""
    server = (s.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    topic = str(s.get("ntfy_topic") or "").strip().strip("/")
    headers = {"Title": "Holdco", "Content-Type": "text/plain; charset=utf-8", "User-Agent": "Holdco"}
    if kind in ("error", "escalation"):
        headers["Priority"] = "high"
    try:
        req = urllib.request.Request(f"{server}/{urllib.parse.quote(topic)}",
                                     data=_plain(text)[:MAX_TEXT].encode("utf-8"), method="POST", headers=headers)
        with _open(req, HTTP_TIMEOUT) as r:
            r.read(2000)
    except urllib.error.HTTPError as e:
        raise NotifyError(f"ntfy: HTTP {e.code} {e.read(2000).decode('utf-8', 'replace')[:200]}") from None
    except Exception as e:
        raise NotifyError(f"ntfy: {getattr(e, 'reason', None) or e}") from None


# ---------------- sending ----------------

def send(text: str, buttons: list[list[dict]] | None = None, kind: str = "info", *, dedupe: bool = True) -> bool:
    """Send one message to every configured channel. Returns True when at least one channel accepted it.

    kind: important | request | escalation | error | brief are sent at notify_level "important"; "info" only at
    "all". Identical text is not resent within REPEAT_SECONDS unless dedupe=False (confirmations of an action the
    owner just took, like the kill switch, must always arrive). Blocks for the HTTP calls: callers that must not
    wait (the hooks below) go through _enqueue. Never raises.
    """
    s: dict = {}
    try:
        s = db.get_settings()
        return _send(s, text, buttons, kind, dedupe)
    except Exception as e:   # a phone problem must never reach an agent or the server
        _record_failure(f"send failed: {e}", s)
        return False


def _send(s: dict, text: str, buttons, kind: str, dedupe: bool) -> bool:
    text = (text or "").strip()
    if not text or not configured(s) or not _wanted(kind, s):
        return False
    key, now = _key(text), time.time()
    if dedupe:
        with _lock:
            if now - _recent.get(key, 0) < REPEAT_SECONDS:
                return False
    ok = False
    if s.get("telegram_bot_token") and _chat_id(s):
        try:
            _send_telegram(s, text, buttons)
            ok = True
        except NotifyError as e:
            _record_failure(str(e), s)
    if s.get("ntfy_topic"):
        try:
            _send_ntfy(s, text, kind)
            ok = True
        except NotifyError as e:
            _record_failure(str(e), s)
    if ok and dedupe:
        with _lock:
            _recent[key] = now
            if len(_recent) > 500:
                for k in [k for k, t in _recent.items() if now - t >= REPEAT_SECONDS]:
                    del _recent[k]
    return ok


# ---------------- hooks ----------------

def _labels() -> dict:
    try:
        from . import actions   # imported here: actions imports this module inside gate()
        return actions.ACTION_LABELS
    except Exception:
        return {"send_email": "Send email", "social_post": "Social post", "deploy_site": "Deploy website",
                "http_post": "Outside request", "run_code": "Run code", "spend_money": "Purchase",
                "question": "Question"}


def _payload_lines(action: str, p: dict) -> list[str]:
    """The fields of a request the owner needs to decide from their phone (HTML-escaped, trimmed)."""
    def g(k):
        v = p.get(k)
        return "" if v is None else str(v).strip()
    if action == "send_email":
        return [f"To: {escape(g('to'))}", f"Subject: {escape(g('subject'))}", escape(g("body")[:300])]
    if action == "social_post":
        lines = [f"{escape((g('platform') or 'social').title())} post:", escape((g("content") or g("text"))[:400])]
        if g("link"):
            lines.append(f"Link: {escape(g('link'))}")
        return lines
    if action == "deploy_site":
        return ["Publish the website from site/"]
    if action == "http_post":
        return [f"{escape((g('method') or 'POST').upper())} {escape(g('url'))}"]
    if action == "run_code":
        code = "\n".join(line[:120] for line in g("code").splitlines()[:6])
        return [f"Run {escape(g('language') or 'python')}:", f"<pre>{escape(code)}</pre>" if code else ""]
    if action == "spend_money":
        vendor = f" ({escape(g('vendor'))})" if g("vendor") else ""
        return [f"Item: {escape(g('item'))}", f"Amount: ${escape(g('amount_usd'))}{vendor}",
                f"Reason: {escape(g('reason'))}" if g("reason") else ""]
    return []


def build_approval(row: dict) -> tuple[str, list[list[dict]] | None, str]:
    """The message, buttons and kind for an approvals row. Pure, so tests can check the rendering."""
    p = db.loads(row.get("payload"), {}) or {}
    company = db.get_company(row["company_id"]) if row.get("company_id") else None
    agent = db.get_agent(row["agent_id"]) if row.get("agent_id") else None
    cname = escape((company or {}).get("name") or "Holdco")
    aname = escape((agent or {}).get("name") or "An agent")
    aid = row["id"]
    action = row.get("action") or ""
    if action == "question":
        options = [str(o).strip() for o in (p.get("options") or []) if str(o).strip()]
        rec = str(p.get("recommended") or "").strip()
        escalation = bool(p.get("pattern"))
        lines = [f"<b>{aname}</b> at <b>{cname}</b> {'needs a decision' if escalation else 'asks'} (question #{aid}):",
                 escape(p.get("question") or row.get("summary") or "")]
        if p.get("context"):
            lines += ["", escape(str(p["context"])[:400])]
        if rec and rec in options:
            lines.append(f"Recommended: {escape(rec)}")
        if options:
            buttons = [[{"text": (CHECK if o == rec else "") + o[:56], "data": f"q:{aid}:{i}"}]
                       for i, o in enumerate(options)]
        else:
            buttons = None
            lines.append("Answer it in the dashboard (Inbox).")
        return "\n".join(lines), buttons, "escalation" if escalation else "request"
    label = _labels().get(action, action.replace("_", " ").title())
    lines = [f"<b>{aname}</b> at <b>{cname}</b> wants to: <b>{escape(label)}</b> (request #{aid})"]
    detail = [ln for ln in _payload_lines(action, p) if ln]
    lines += detail or [escape((row.get("summary") or "")[:300])]
    buttons = [[{"text": "Approve", "data": f"a:{aid}:approve"}, {"text": "Reject", "data": f"a:{aid}:reject"}]]
    return "\n".join(lines), buttons, "request"


def approval_requested(approval: dict) -> None:
    """Hook from actions.gate when a request is queued: message with Approve/Reject or option buttons.

    The message is built here (a few cheap database reads, and a broken row is reported at once); the HTTP
    sends run on the sender thread, so gate() returns to the agent before the phone is reached.
    """
    if not approval:
        return
    try:
        if not configured():
            return
        text, buttons, kind = build_approval(approval)
    except Exception as e:
        _record_failure(f"could not announce request #{approval.get('id')}: {e}")
        return
    _enqueue(send, text, buttons, kind)


def _error_key(message: str) -> str:
    """The dedupe key for an error: the watchdog's normaliser when it has one, ours otherwise."""
    try:
        from . import watchdog
        fn = getattr(watchdog, "normalise_error", None)
        if fn:
            return str(fn(message) or "")
    except Exception:
        pass
    return normalise(message)


def error(company: dict | None, message: str) -> None:
    """Hook for error events: one message per distinct error per hour. The once-an-hour decision is made
    here, synchronously, so it is deterministic; the send itself runs on the sender thread. Never raises."""
    try:
        if not configured():
            return
        key = _error_key(message)
        if not key:
            return
        now = time.time()
        with _lock:
            if now - _errors.get(key, 0) < ERROR_REPEAT_SECONDS:
                return
            _errors[key] = now
            if len(_errors) > 500:
                for k in [k for k, t in _errors.items() if now - t >= ERROR_REPEAT_SECONDS]:
                    del _errors[k]
        where = f" at <b>{escape(company['name'])}</b>" if company and company.get("name") else ""
        _enqueue(send, f"<b>Error</b>{where}: {escape(str(message)[:600])}", None, "error")
    except Exception as e:
        _record_failure(f"error hook failed: {e}")


def set_kill_switch(on: bool, by: str = "owner") -> bool:
    """Flip the kill switch: save it, emit the event, refresh the dashboard and confirm on the phone.

    Shared by POST /api/kill and the Telegram /stop and /resume commands. The confirmation skips the
    identical-text rule (dedupe=False), so switching ON twice within an hour is confirmed both times, and it
    goes through the sender thread so the dashboard button answers before Telegram does.
    """
    on = bool(on)
    db.set_settings({"kill_switch": on})
    bus.emit(None, "info", "Kill switch ON: all agents stopped and outbound actions frozen." if on
             else "Kill switch off: agents may work again.", None, {"by": by})
    bus.notify("settings")
    _enqueue(send, KILL_ON_TEXT if on else KILL_OFF_TEXT, None, "important", dedupe=False)
    return on


STATUS_MAX_COMPANIES = 15   # companies listed by /status; the summary lines always come first


def format_status() -> str:
    """The /status reply: the summary first (kill switch, requests waiting, Doctor), then one line per company,
    busiest first. Telegram caps a message at 4096 characters, so with many companies the list is cut, never
    the summary."""
    s = db.get_settings()
    rows, pending_total = [], 0
    for c in db.q("SELECT * FROM companies WHERE status!='archived' ORDER BY id"):
        open_n = db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN "
                           "('todo','in_progress','blocked','waiting')", (c["id"],)) or 0
        working = db.scalar("SELECT COUNT(*) FROM agents WHERE company_id=? AND status='working'", (c["id"],)) or 0
        pend = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pending'", (c["id"],)) or 0
        pending_total += pend
        rows.append((pend, working, open_n, c))
    lines = ["Kill switch: " + ("ON (send /resume to turn it off)" if s.get("kill_switch") else "off")
             + ("" if s.get("scheduler_enabled", True) else "; autopilot is off"),
             f"Requests waiting for you: {pending_total}"]
    try:
        from . import doctor
        d = doctor.summary()
        if d.get("checked_at"):
            lines.append(f"Doctor: {d['ok']} ok, {d['warn']} warn, {d['bad']} bad")
    except Exception:
        pass
    if not rows:
        lines.append("No companies yet.")
    rows.sort(key=lambda r: (-r[0], -r[1], -r[2], r[3]["id"]))
    for pend, working, open_n, c in rows[:STATUS_MAX_COMPANIES]:
        lines.append(f"<b>{escape(c['name'])}</b> ({escape(c['status'])}): {open_n} open tasks, {working} working, "
                     f"{pend} request{'s' if pend != 1 else ''} waiting")
    if len(rows) > STATUS_MAX_COMPANIES:
        lines.append(f"...and {len(rows) - STATUS_MAX_COMPANIES} more in the dashboard.")
    return "\n".join(lines)


def apply_callback(data: str) -> str:
    """Turn a button press into a decision. Returns the outcome text; raises ValueError with a short reason."""
    from . import actions   # imported here: actions imports this module inside gate()
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] not in ("a", "q") or not parts[1].isdigit():
        raise ValueError("That button is not recognised")
    aid = int(parts[1])
    if parts[0] == "a":
        if parts[2] not in ("approve", "reject"):
            raise ValueError("That button is not recognised")
        actions.decide(aid, parts[2])
        return "Approved" if parts[2] == "approve" else "Rejected"
    row = db.one("SELECT * FROM approvals WHERE id=?", (aid,))
    if not row:
        raise ValueError("Request not found")
    options = [str(o) for o in (db.loads(row.get("payload"), {}) or {}).get("options") or []]
    if not parts[2].isdigit() or int(parts[2]) >= len(options):
        raise ValueError("That option no longer exists")
    answer = options[int(parts[2])]
    actions.decide(aid, "approve", answer=answer)   # decide treats a question as answered
    return answer


# ---------------- long polling ----------------

class Poller(threading.Thread):
    """Long-polls getUpdates and turns button presses and commands into actions. One per process.

    The thread captures the bot settings it started with; when the owner changes them it exits on its next
    loop and ensure_poller() starts a fresh one. It keeps running while the kill switch is on so /resume works.
    """

    def __init__(self, settings: dict | None = None):
        super().__init__(daemon=True, name="telegram-poller")
        s = settings or db.get_settings()
        self.token = s.get("telegram_bot_token") or ""
        self.chat_id = _chat_id(s)
        self.base = (s.get("telegram_api_base") or "").rstrip("/")
        self.stop_event = threading.Event()
        self.offset = 0
        self.backoff = BACKOFF_MIN
        self.handled = 0           # updates processed so far (tests wait on it)
        self.last_error = ""

    def stop(self, timeout: float | None = None) -> None:
        self.stop_event.set()
        if timeout is not None and self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def _settings_changed(self, s: dict) -> bool:
        return (self.token != (s.get("telegram_bot_token") or "") or self.chat_id != _chat_id(s)
                or self.base != (s.get("telegram_api_base") or "").rstrip("/"))

    def run(self) -> None:
        while not self.stop_event.is_set():
            s = db.get_settings()
            if not (self.token and self.chat_id) or self._settings_changed(s):
                return   # the owner changed the bot settings: ensure_poller() starts a new thread when needed
            try:
                updates = _telegram(s, METHODS["get_updates"],
                                    {"offset": self.offset, "timeout": POLL_TIMEOUT,
                                     "allowed_updates": ["message", "callback_query"]},
                                    timeout=POLL_TIMEOUT + 15)
                self.backoff = BACKOFF_MIN
            except Exception as e:
                self.last_error = scrub(e, s)
                _record_failure(f"poller: {self.last_error}", s)
                wait, self.backoff = self.backoff, min(self.backoff * 2, BACKOFF_MAX)
                self.stop_event.wait(wait)   # a stop() interrupts the wait at once
                continue
            for u in updates or []:
                try:
                    self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                    self._handle_update(s, u)
                except Exception as e:
                    print(f"notify: poller could not handle an update: {scrub(e, s)}")
                self.handled += 1

    # Not '_handle': Python 3.13's Thread keeps its own internal _handle attribute, which would replace
    # this method and make every update fail.
    def _handle_update(self, s: dict, u: dict) -> None:
        cq = u.get("callback_query")
        if cq:
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {}).get("id", (cq.get("from") or {}).get("id"))
            if str(chat) == self.chat_id:   # anyone else who finds the bot is ignored
                self._callback(s, cq)
            return
        msg = u.get("message") or u.get("edited_message")
        if msg and str((msg.get("chat") or {}).get("id")) == self.chat_id:
            self._command(s, msg.get("text") or "")

    def _reply(self, s: dict, text: str) -> None:
        try:
            _send_telegram(s, text)
        except NotifyError as e:
            _record_failure(str(e), s)

    def _callback(self, s: dict, cq: dict) -> None:
        data = str(cq.get("data") or "")
        msg = cq.get("message") or {}
        outcome, problem = "", ""
        if data == "noop":
            outcome = "OK"
        else:
            try:
                outcome = apply_callback(data)
            except Exception as e:   # already decided, kill switch, unknown button: tell the owner, keep polling
                problem = str(e)
        try:
            _telegram(s, METHODS["answer_callback"],
                      {"callback_query_id": cq.get("id"), "text": (problem or outcome)[:190]})
        except NotifyError as e:
            _record_failure(str(e), s)
        if problem:
            self._reply(s, f"Could not do that: {escape(problem)}")
            return
        if msg.get("message_id") is None:
            return
        chat, mid = (msg.get("chat") or {}).get("id", self.chat_id), msg["message_id"]
        text = _original_html(chat, mid, msg.get("text") or "") + f"\n\n<b>You: {escape(outcome)}</b>"
        for method, body in ((METHODS["edit_markup"], {"chat_id": chat, "message_id": mid,
                                                       "reply_markup": {"inline_keyboard": []}}),
                             (METHODS["edit_text"], {"chat_id": chat, "message_id": mid, "text": text[:MAX_TEXT],
                                                     "parse_mode": "HTML", "disable_web_page_preview": True})):
            try:
                _telegram(s, method, body)
            except NotifyError as e:
                _record_failure(str(e), s)

    def _command(self, s: dict, text: str) -> None:
        words = text.strip().split()
        cmd = words[0].lower().split("@")[0] if words else ""
        if cmd == "/status":
            self._reply(s, format_status())
        elif cmd == "/stop":
            set_kill_switch(True, by="telegram")   # the confirmation is the broadcast every channel gets
        elif cmd == "/resume":
            set_kill_switch(False, by="telegram")
        else:
            self._reply(s, HELP_TEXT)


def ensure_poller() -> None:
    """Start (or keep alive) the Telegram long-polling thread when a token and chat id are set."""
    global POLLER
    s = db.get_settings()
    if not (s.get("telegram_bot_token") and _chat_id(s)):
        return   # a running thread notices the change on its next loop and exits by itself
    with _poller_lock:
        if POLLER is not None and POLLER.is_alive():
            return
        POLLER = Poller(s)
        POLLER.start()


def stop_poller(timeout: float = 5.0) -> None:
    """Stop the polling thread (tests and shutdown); waits up to timeout seconds for it to finish."""
    global POLLER
    with _poller_lock:
        p, POLLER = POLLER, None
    if p is not None:
        p.stop(timeout)


# ---------------- health and the test button ----------------

def check_telegram(settings: dict | None = None) -> tuple[bool, str]:
    """getMe with the configured token: (True, "Telegram bot @name") or (False, why). The reason never
    carries the token: _telegram scrubs its own errors and anything else is scrubbed here."""
    s = settings or db.get_settings()
    try:
        me = _telegram(s, METHODS["get_me"], {}, timeout=10) or {}
    except Exception as e:
        return False, scrub(e, s)
    name = me.get("username") or me.get("first_name") or "bot"
    return True, f"Telegram bot @{name}"


def health(settings: dict | None = None) -> tuple[str, str]:
    """(status, detail) for the Doctor: ok | warn | bad. warn when nothing is configured or a send failed lately."""
    s = settings or db.get_settings()
    token, chat, topic = s.get("telegram_bot_token") or "", _chat_id(s), str(s.get("ntfy_topic") or "").strip()
    if not token and not topic:
        return "warn", ("No phone notifications set up. Add a Telegram bot token and chat id, or an ntfy topic, "
                        "in Settings.")
    status, parts = "ok", []
    if token:
        ok, detail = check_telegram(s)
        if not ok:
            status = "bad"
        elif not chat:
            status = "warn"
            detail += ", but no chat id is set: message the bot, then press Send test message to find it"
        parts.append(detail)
    if topic:
        parts.append(f"ntfy topic set on {(s.get('ntfy_server') or 'https://ntfy.sh').rstrip('/')}")
    last_at = _failures.get("last_at")
    if status == "ok" and last_at and time.time() - last_at < 3600:
        status = "warn"
        parts.append(f"last failure: {_failures['last_error']}")
    return status, scrub("; ".join(parts), s)


def discover_chat_ids(s: dict | None = None) -> list:
    """Chat ids that have messaged the bot (one getUpdates), for the owner who has not found theirs yet.
    Anyone who finds the bot can message it, so the list is shown, never trusted or saved by itself."""
    s = s or db.get_settings()
    try:
        updates = _telegram(s, METHODS["get_updates"], {"timeout": 0}, timeout=10) or []
    except Exception:
        return []
    ids = []
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or (u.get("callback_query") or {}).get("message") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid is not None and cid not in ids:
            ids.append(cid)
    return ids


@route("POST", "/api/notify/test")
def api_notify_test(h, m, body):
    """Send the test message to every configured channel; with a token but no chat id, list the chat ids seen."""
    s = db.get_settings()
    if s.get("telegram_bot_token") and not _chat_id(s):
        ids = discover_chat_ids(s)
        if ids:
            detail = (f"Chat ids that have messaged the bot: {', '.join(str(i) for i in ids)}; make sure the one "
                      "you save is yours. Paste it into the Telegram chat id field, save, then press this again.")
        else:
            detail = "Send any message to your bot in Telegram, then press this again and copy your chat id."
        return {"ok": False, "detail": detail, "message": detail, "chat_ids": ids}
    if not configured(s):
        detail = "Nothing to send to yet. Add a Telegram bot token and chat id, or an ntfy topic, then save."
        return {"ok": False, "detail": detail, "message": detail, "chat_ids": []}
    ok = send(TEST_TEXT, [[{"text": "OK", "data": "noop"}]], kind="important", dedupe=False)
    detail = "Sent. Check your phone." if ok else f"Could not send: {_failures.get('last_error') or 'unknown error'}"
    detail = scrub(detail, s)
    return {"ok": ok, "detail": detail, "message": detail, "chat_ids": []}
