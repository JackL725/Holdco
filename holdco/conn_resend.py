"""The resend connector (Phase 1, docs/phases/phase-1-connect.md section 7).

Outbound email through Resend's API (POST /emails, Bearer re_..., JSON body, optional Idempotency-Key, answer
{id}). Agents keep using send_email: emailer.send picks Resend or SMTP from the company's email_provider setting
(auto | smtp | resend). Inbound mail stays on IMAP because Resend's inbound is webhook-based and Holdco opens no
ports. No tools of its own.

Metrics: email.sent_7d and email.replies_7d come from the emails table (any provider), so the definition carries
metric_prefix "email" instead of its own key.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.resend.com"
TIMEOUT = 30
HEALTH_TIMEOUT = 15
_KEY_RE = re.compile(r"\bre_[A-Za-z0-9_]{6,}")
_PREFIX_RE = re.compile(r"^\s*((re|fwd?|aw|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_proxied = urllib.request.build_opener()


class ResendError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _key(inst: dict | None) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get("api_key") or "").strip()


def _base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or API_BASE).rstrip("/")


def from_address(inst: dict | None) -> str:
    return str(_cfg(inst).get("from_address") or "").strip()


def from_name(inst: dict | None) -> str:
    return str(_cfg(inst).get("from_name") or "").strip()


def _scrub(text: str, key: str = "") -> str:
    text = str(text or "")
    if key and len(key) >= 6:
        text = text.replace(key, "[key]")
    return _KEY_RE.sub("[key]", text)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request(inst: dict, method: str, path: str, body: dict | None = None, headers: dict | None = None,
             timeout: float = TIMEOUT) -> tuple[int, dict | list, str]:
    """One call. Returns (status, parsed JSON, raw text prefix); network failures raise ResendError."""
    key = _key(inst)
    if not key:
        raise ResendError("No Resend key is set. Add one on the Resend card in the company's Settings.")
    url = _base(inst) + path
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": f"Bearer {key}", "User-Agent": "Holdco", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(500_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(50_000)
    except urllib.error.URLError as e:
        raise ResendError(_scrub(f"Cannot reach Resend: {e.reason}", key)) from None
    except (TimeoutError, socket.timeout):
        raise ResendError("Resend did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise ResendError(_scrub(f"Cannot reach Resend: {e}", key)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text or "{}")
    except ValueError:
        parsed = {}
    return status, parsed, _scrub(text[:300], key)


def _error_message(parsed, raw: str) -> str:
    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("name") or raw)[:300]
    return raw[:300]


def send(inst: dict, company: dict | None, *, sender: str, to: str, subject: str, text: str, reply_to: str = "",
         headers: dict | None = None, idempotency_key: str = "") -> str:
    """POST /emails. Returns Resend's id. A repeat with the same idempotency key does not send twice."""
    body = {"from": sender, "to": to, "subject": subject, "text": text}
    if reply_to:
        body["reply_to"] = reply_to
    if headers:
        body["headers"] = headers
    extra = {"Idempotency-Key": idempotency_key[:256]} if idempotency_key else {}
    status, parsed, raw = _request(inst, "POST", "/emails", body, extra)
    if status >= 400:
        raise ResendError(f"Resend refused the email: HTTP {status}: {_error_message(parsed, raw)}", status)
    rid = parsed.get("id") if isinstance(parsed, dict) else None
    if not rid:
        raise ResendError(f"Resend answered without an id: {raw[:200]}", status)
    return str(rid)


def health(inst: dict) -> tuple[str, str]:
    if not _key(inst):
        return "bad", "No key: paste a Resend API key (re_...)"
    addr = from_address(inst)
    try:
        status, parsed, raw = _request(inst, "GET", "/domains", timeout=HEALTH_TIMEOUT)
    except ResendError as e:
        return "bad", str(e)[:300]
    if status in (401, 403):
        if "restricted" in raw.lower():
            # A sending-only key is valid but may not list domains, so verification cannot be seen from here.
            return "ok", ("sending-only key: Holdco cannot see whether the domain is verified; check it in "
                          "Resend")
        return "bad", "key rejected"
    if status >= 400:
        return "bad", f"Resend answered HTTP {status}: {_error_message(parsed, raw)}"
    if not addr or "@" not in addr:
        return "warn", "key works; add the from address (on a domain you verified in Resend)"
    domain = addr.rsplit("@", 1)[1].lower()
    rows = parsed.get("data") if isinstance(parsed, dict) else parsed
    for d in rows or []:
        if isinstance(d, dict) and str(d.get("name") or "").lower() == domain:
            state = str(d.get("status") or "unknown")
            if state == "verified":
                return "ok", f"{domain} verified"
            return "warn", f"{domain} is {state} in Resend; finish verifying it (DNS records) before sending"
    return "warn", f"{domain} is not added in Resend; add and verify it, or use an address on a verified domain"


# ---------------------------------------------------------------- metrics (from the emails table)

def _iso(days_ago: float) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def normal_subject(subject: str) -> str:
    """A subject without Re:/Fwd: prefixes, spacing and case, so a reply matches what was sent."""
    return " ".join(_PREFIX_RE.sub("", subject or "").split()).lower()


def _addr(value: str) -> str:
    return (email.utils.parseaddr(value or "")[1] or value or "").strip().lower()


def sent_7d(company_id: int) -> int:
    from . import db
    return int(db.scalar("SELECT COUNT(*) FROM emails WHERE company_id=? AND direction='out' AND created_at>=?",
                         (company_id, _iso(7))) or 0)


def replies_7d(company_id: int) -> tuple[int, int, int]:
    """Inbound emails in the last 7 days that answer something the company sent: by In-Reply-To first, else by
    subject (without Re:/Fwd:) sent to that address in the last 30 days. Returns (total, by header, by subject)."""
    from . import db
    out = db.q("SELECT message_id, to_addr, subject FROM emails WHERE company_id=? AND direction='out' "
               "AND created_at>=?", (company_id, _iso(30)))
    ids = {str(r["message_id"] or "").strip() for r in out if r["message_id"]}
    pairs = {(_addr(r["to_addr"]), normal_subject(r["subject"])) for r in out}
    by_header = by_subject = 0
    for r in db.q("SELECT from_addr, subject, in_reply_to FROM emails WHERE company_id=? AND direction='in' "
                  "AND created_at>=?", (company_id, _iso(7))):
        refs = str(r.get("in_reply_to") or "").split()
        if any(x.strip() in ids for x in refs):
            by_header += 1
        elif (_addr(r["from_addr"]), normal_subject(r["subject"])) in pairs:
            by_subject += 1
    return by_header + by_subject, by_header, by_subject


def metrics(inst: dict, company: dict | None) -> list[tuple[str, float, str]]:
    if not company:
        return []
    total, by_header, by_subject = replies_7d(company["id"])
    return [("sent_7d", float(sent_7d(company["id"])), "outbound emails in 7 days (SMTP and Resend)"),
            ("replies_7d", float(total),
             f"inbound replies in 7 days: {by_header} by In-Reply-To, {by_subject} by subject")]


def tools(inst: dict | None) -> list[dict]:
    return []   # agents keep using send_email (its policy, daily cap and outreach checks stay in force)


DEFINITION = {
    "label": "Resend",
    "category": "Email",
    "kind": "api",
    "scope": "company",
    "metric_prefix": "email",   # its metrics are email.sent_7d and email.replies_7d
    "setup": {
        "where_to_get_key": ("In Resend, add and verify your sending domain (Domains), then API Keys > Create API "
                             "key. Paste it here with a from address on that domain. Set the company's "
                             "email_provider to resend, or leave it on auto to use Resend whenever this card is "
                             "healthy."),
        "permissions": ["Sending access is enough to send; full access also lets Holdco check that the domain "
                        "is verified"],
        "fields": [
            {"key": "api_key", "label": "API key", "secret": True, "hint": "Starts with re_",
             "placeholder": "re_..."},
            {"key": "from_address", "label": "From address", "secret": False,
             "hint": "An address on a domain you verified in Resend", "placeholder": "hello@yourdomain.com"},
            {"key": "from_name", "label": "From name", "secret": False,
             "hint": "Blank uses the company name", "placeholder": "PawPlan"},
            {"key": "api_base", "label": "API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": API_BASE},
        ],
        "test_mode": ("Resend has no test mode. Keep the send_email policy on approve: every email waits for you "
                      "in the Inbox until you are sure."),
        "docs_url": "https://resend.com/docs/api-reference/emails/send-email",
        "agents_can": ["Send the emails you approve through Resend (send_email, same rules and daily cap)",
                       "Replies still arrive through the company's IMAP mailbox"],
    },
    "health": health,
    "metrics": metrics,
    "tools": tools,
}
