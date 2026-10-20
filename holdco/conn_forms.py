"""The forms connector (Phase 4, docs/phases/phase-4-scale.md section 3): signups from the site's forms, polled.

Two modes, chosen on the card:
- netlify: Netlify Forms, read with the company's Netlify token (the one the deploy uses, from the vault) and its
  netlify_site_id (or the site id on this card). GET <api>/sites/<site>/forms finds the form by its name
  ({id, name, submission_count, fields: [{name}]}); GET <api>/forms/<form id>/submissions?per_page=&page= lists its
  submissions newest first ({id, created_at, email, name, first_name, last_name, company, data: {...}}).
- worker: a Cloudflare Worker made from docs/forms-worker.js, which stores the form posts and serves them to
  GET <endpoint_url>?form=<name>&after=<id>&since=<ISO time>&limit= with the X-Holdco-Secret header, answering
  {"submissions": [{id, form, created_at, data}] oldest first, "total", "last_7_days", "more"} (form: only the card's
  form, when it names one).

Every hour (the framework's metric pipe), submissions newer than the last one seen become contacts: stage lead,
source forms:<form name>, consent from a consent field (a form that has one but whose box was left unticked gives
"no"; a form without one leaves consent empty and says so in the notes), one contact per email address. An address on
the do-not-email list (verify.is_suppressed) is skipped, and so is one the company already has. The poll also stores
forms.signups (the last 7 days) and forms.signups_total with source forms. Holdco writes only fixed text into a
contact's notes: what people typed into a message box never reaches the contact list.

forms__recent_signups(days) [read] lists the latest signups without their addresses (the contact list holds them,
shown only to the roles that message people). Names are what people typed, so the tool counts as outside content
and reading it taints the session (Phase 3).

The Netlify paths and answer shapes are named in the constants below; verify them against the Netlify Forms API docs
before connecting.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30            # seconds for one API call
HEALTH_TIMEOUT = 20
WINDOW_DAYS = 7         # forms.signups counts this many days
PER_PAGE = 100          # Netlify submissions per page
MAX_PAGES = 20          # most pages (Netlify) or batches (Worker) one poll reads
MAX_SHOWN = 30          # rows recent_signups shows
MODES = ("netlify", "worker")

# Netlify (verify against the vendor docs: the Forms API)
NETLIFY_API = "https://api.netlify.com/api/v1"
NETLIFY_FORMS = "/sites/{site}/forms"
NETLIFY_SUBMISSIONS = "/forms/{form}/submissions"
# The Worker (docs/forms-worker.js; Holdco defines both sides)
SECRET_HEADER = "X-Holdco-Secret"

_TRUE = ("yes", "y", "true", "on", "1", "checked", "agree", "agreed", "accept", "accepted", "ok", "i agree")
_FALSE = ("no", "n", "false", "off", "0", "none", "")
# A consent answer that says no in words: "No thanks", "Not now", "I don't agree", "I do not consent", "Decline".
_NO_RE = re.compile(r"^(?:i\s+)?(?:no|nope|not|never|don'?t|do\s+not|decline[ds]?|disagree|opt[\s-]?out|"
                    r"unsubscribe)\b")
NO_CONSENT_FIELD = ("Consent unknown: the form has no consent field. Ask the owner before sending this person "
                    "anything beyond a direct reply.")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # fakes on 127.0.0.1 skip any proxy
_proxied = urllib.request.build_opener()


class FormsError(RuntimeError):
    """A forms call failed. `status` is the HTTP status (0 when the service could not be reached)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- helpers

def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def mode(inst: dict | None) -> str:
    m = str(_cfg(inst).get("mode") or "").strip().lower()
    return m or "netlify"


def _form_name(inst: dict | None) -> str:
    return str(_cfg(inst).get("form_name") or "").strip()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def parse_time(s) -> _dt.datetime | None:
    """An ISO time from Netlify or the Worker (2026-10-17T10:00:00.000Z, with or without a fraction or offset), in
    UTC; None when it cannot be read."""
    text = str(s or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", text)
    if not m:
        return None
    try:
        t = _dt.datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    off = m.group(3)
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        t -= sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return t.replace(tzinfo=_dt.timezone.utc)


def _local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _scrub(text: str, secrets: list[str]) -> str:
    text = str(text or "")
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[secret]")
    return text


def _get(url: str, headers: dict, secrets: list[str], service: str, timeout: float = TIMEOUT):
    """One JSON GET. Returns (status, parsed answer, text prefix); a network failure raises FormsError."""
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "Holdco",
                                                              **headers})
    opener = _direct if _local(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(5_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(50_000)
    except urllib.error.URLError as e:
        raise FormsError(_scrub(f"Cannot reach {service}: {e.reason}", secrets)) from None
    except (TimeoutError, socket.timeout):
        raise FormsError(f"{service} did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise FormsError(_scrub(f"Cannot reach {service}: {e}", secrets)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        parsed = None
    return status, parsed, _scrub(text[:300], secrets)


def _message(parsed, text: str) -> str:
    if isinstance(parsed, dict) and (parsed.get("message") or parsed.get("error")):
        return str(parsed.get("message") or parsed.get("error"))[:200]
    return text[:200]


def _value(v) -> str:
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return " ".join(str(v if v is not None else "").split())[:500]


def _sub(sid, created, form: str, data: dict, top: dict | None = None) -> dict:
    """One submission in Holdco's shape: {id, created_at (datetime or None), form, data (str values)}."""
    fields = {str(k): _value(v) for k, v in (data or {}).items() if isinstance(k, str)}
    for k, v in (top or {}).items():   # Netlify's top-level email, name, ... fill gaps in data
        if v not in (None, "") and not fields.get(k):
            fields[k] = _value(v)
    return {"id": str(sid or ""), "created_at": parse_time(created), "form": form, "data": fields}


# ---------------------------------------------------------------- Netlify

def _company_of(inst: dict, company: dict | None) -> dict | None:
    if company is not None:
        return company
    if inst.get("company_id"):
        from . import db
        return db.get_company(int(inst["company_id"]))
    return None


def _netlify_auth(inst: dict, company: dict | None) -> tuple[str, str]:
    """(token, site id): the company's Netlify token and site, or the site id on this card."""
    from . import db
    c = _company_of(inst, company)
    cs = db.company_settings(c) if c else {}
    token = str(cs.get("netlify_token") or "").strip()
    site = str(_cfg(inst).get("netlify_site_id") or "").strip() or str(cs.get("netlify_site_id") or "").strip()
    if not token:
        raise FormsError("No Netlify token: add it in the company's Settings (the one the site deploys with).")
    if not site:
        raise FormsError("No Netlify site yet: deploy the site once, or put its site id on the forms card.")
    return token, site


def _netlify_base(inst: dict) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or NETLIFY_API).rstrip("/")


def _netlify(inst: dict, token: str, path: str, params: dict | None = None, timeout: float = TIMEOUT):
    url = _netlify_base(inst) + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    status, parsed, text = _get(url, {"Authorization": f"Bearer {token}"}, [token], "Netlify", timeout)
    if status in (401, 403):
        raise FormsError(f"Netlify refused the token (HTTP {status}): check the company's Netlify token.", status)
    if status == 404:
        raise FormsError("Netlify does not know that site: check the site id.", status)
    if status >= 400 or parsed is None:
        raise FormsError(f"Netlify answered HTTP {status}: {_message(parsed, text)}", status)
    return parsed


def _netlify_form(inst: dict, company: dict | None, timeout: float = TIMEOUT) -> tuple[str, dict]:
    """(token, form) for the card's form name."""
    token, site = _netlify_auth(inst, company)
    name = _form_name(inst)
    if not name:
        raise FormsError("Set the form name: the name attribute of the form on the site.")
    forms = _netlify(inst, token, NETLIFY_FORMS.format(site=urllib.parse.quote(site, safe="")), timeout=timeout)
    forms = [f for f in (forms if isinstance(forms, list) else []) if isinstance(f, dict)]
    form = next((f for f in forms if str(f.get("name") or "").lower() == name.lower()), None)
    if form is None:
        names = ", ".join(sorted(str(f.get("name")) for f in forms)) or "none"
        raise FormsError(f"No form named {name} on the Netlify site (forms there: {names}). Netlify finds a form "
                         "that has data-netlify=\"true\" and a name when the site deploys.")
    return token, form


def _netlify_subs(inst: dict, token: str, form: dict):
    """The form's submissions, newest first, page by page (at most MAX_PAGES pages)."""
    path = NETLIFY_SUBMISSIONS.format(form=urllib.parse.quote(str(form.get("id") or ""), safe=""))
    name = str(form.get("name") or "")
    for page in range(1, MAX_PAGES + 1):
        rows = _netlify(inst, token, path, {"per_page": PER_PAGE, "page": page})
        rows = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
        for r in rows:
            top = {k: r.get(k) for k in ("email", "name", "first_name", "last_name", "company")}
            yield _sub(r.get("id"), r.get("created_at"), str(r.get("form_name") or name),
                       r.get("data") if isinstance(r.get("data"), dict) else {}, top)
        if len(rows) < PER_PAGE:
            return


def _has_consent_field(form: dict) -> bool:
    return any(_is_consent(str((f or {}).get("name") or "")) for f in (form.get("fields") or [])
               if isinstance(f, dict))


# ---------------------------------------------------------------- the Worker

def _worker(inst: dict, params: dict, timeout: float = TIMEOUT) -> dict:
    from . import connectors   # imported here: connectors imports the conn_* modules
    url = str(_cfg(inst).get("endpoint_url") or "").strip()
    secret = str((connectors.secrets(inst) or {}).get("shared_secret") or "")
    if not re.match(r"^https?://", url):
        raise FormsError("Set the Worker's address (endpoint_url), starting with https://.")
    if not secret:
        raise FormsError("Set the shared secret: the SHARED_SECRET value of the Worker.")
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    status, parsed, text = _get(full, {SECRET_HEADER: secret}, [secret], "The forms Worker", timeout)
    if status in (401, 403):
        raise FormsError("The forms Worker refused the shared secret.", status)
    if status >= 400 or not isinstance(parsed, dict) or not isinstance(parsed.get("submissions"), list):
        raise FormsError(f"The forms Worker answered HTTP {status}: {_message(parsed, text)}", status)
    return parsed


def _worker_subs(inst: dict, after: str = "", since: _dt.datetime | None = None):
    """(submissions oldest first, total, last 7 days) after an id or since a time, batch by batch."""
    want = _form_name(inst).lower()
    out: list[dict] = []
    total = recent = 0
    for _ in range(MAX_PAGES):
        params = {"limit": 500}
        if want:
            params["form"] = want
        if after:
            params["after"] = after
        if since:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        answer = _worker(inst, params)
        total, recent = int(answer.get("total") or 0), int(answer.get("last_7_days") or 0)
        batch = [r for r in answer["submissions"] if isinstance(r, dict) and r.get("id")]
        for r in batch:
            form = str(r.get("form") or want or "worker")
            if want and form.lower() != want:
                continue
            out.append(_sub(r.get("id"), r.get("created_at"), form,
                            r.get("data") if isinstance(r.get("data"), dict) else {}))
        if not batch or not answer.get("more"):
            break
        after = str(batch[-1]["id"])
    return out, total, recent


# ---------------------------------------------------------------- contacts

def _is_consent(name: str) -> bool:
    return "consent" in name.lower()


def _field(data: dict, *names: str) -> str:
    low = {k.lower().replace("-", "_").replace(" ", "_"): v for k, v in data.items()}
    for n in names:
        if low.get(n):
            return str(low[n])
    return ""


def email_of(data: dict) -> str:
    """The submission's email address: the email field, else any field whose name says email."""
    v = _field(data, "email", "e_mail", "email_address", "your_email")
    if not v:
        v = next((str(x) for k, x in data.items() if "mail" in k.lower() and _EMAIL_RE.fullmatch(str(x).strip())),
                 "")
    v = v.strip()
    return v if _EMAIL_RE.fullmatch(v) else ""


def name_of(data: dict) -> str:
    name = _field(data, "name", "full_name", "your_name")
    if not name:
        name = " ".join(x for x in (_field(data, "first_name", "firstname"), _field(data, "last_name", "lastname"))
                        if x)
    return name[:80]


def consent_of(data: dict, form_has_field: bool | None) -> str | None:
    """"yes" or "no" from a consent field; "no" when the form has one but it is missing (an unticked box is not
    sent); None when the form has none (or nothing says whether it has)."""
    value = next((v for k, v in data.items() if _is_consent(k)), None)
    if value is None:
        return "no" if form_has_field else None
    v = str(value).strip().lower()
    if v in _FALSE or _NO_RE.match(v):   # a select or text box answered "No thanks", "I do not agree", ...
        return "no"
    return "yes" if v in _TRUE or v else "no"


def to_contacts(company: dict, subs: list[dict], form_has_field: bool | None = None) -> dict:
    """Save submissions as lead contacts. Returns {"added": [contact ids], "known", "suppressed", "no_email"}."""
    from . import brain, verify   # imported here: brain imports tools, which imports the connector framework
    cid = company["id"]
    out = {"added": [], "known": 0, "suppressed": 0, "no_email": 0}
    for s in subs:
        data = s.get("data") or {}
        email = email_of(data)
        if not email:
            out["no_email"] += 1
            continue
        if verify.is_suppressed(email):
            out["suppressed"] += 1
            continue
        if brain._find_contact(cid, email):
            out["known"] += 1
            continue
        consent = consent_of(data, form_has_field)
        when = s["created_at"].strftime("%Y-%m-%d") if s.get("created_at") else "an unknown date"
        notes = f"Signed up through the {s['form']} form on {when}."
        if consent is None:
            notes += " " + NO_CONSENT_FIELD
        row = {"name": name_of(data), "email": email, "company": _field(data, "company", "organization",
                                                                         "organisation")[:120],
               "stage": "lead", "source": f"forms:{s['form']}"[:80], "consent": consent or "", "notes": notes}
        try:
            fields = brain._clean_fields("contacts", row, cid)
            out["added"].append(brain._insert("contacts", cid, fields))
        except ValueError as e:   # a malformed address, or the same person twice in one batch
            print(f"Forms: submission {s.get('id')} not saved as a contact: {e}")
            out["known"] += 1
    return out


# ---------------------------------------------------------------- the poll

def _where(inst: dict, company: dict | None) -> str:
    """What the saved position refers to: a new mode, site, Worker or form starts afresh."""
    if mode(inst) == "worker":
        return f"worker:{str(_cfg(inst).get('endpoint_url') or '').strip()}:{_form_name(inst).lower()}"
    site = str(_cfg(inst).get("netlify_site_id") or "").strip()
    if not site:
        from . import db
        c = _company_of(inst, company)
        site = str(db.company_settings(c).get("netlify_site_id") or "") if c else ""
    return f"netlify:{site}:{_form_name(inst).lower()}"


def _cursor(inst: dict, company: dict | None) -> str:
    cur = _cfg(inst).get("cursor")
    if isinstance(cur, dict) and cur.get("where") == _where(inst, company):
        return str(cur.get("id") or "")
    return ""


def _save_cursor(inst: dict, company: dict | None, last_id: str) -> None:
    """Remember the newest submission seen, in the instance's config (re-read first: the card may have been saved
    meanwhile)."""
    from . import connectors, db
    fresh = connectors.get(inst["id"]) if inst.get("id") else None
    if not fresh:
        return
    cfg = dict(fresh["config"])
    cfg["cursor"] = {"where": _where(fresh, company), "id": last_id}
    db.update("connectors", fresh["id"], config=json.dumps(cfg))


def _new_netlify(inst: dict, company: dict | None, cursor: str) -> tuple[list[dict], int, int, bool]:
    """(new submissions oldest first, last 7 days, total, the form has a consent field)."""
    token, form = _netlify_form(inst, company)
    since = _now() - _dt.timedelta(days=WINDOW_DAYS)
    new, recent, seen, count = [], 0, False, 0
    for s in _netlify_subs(inst, token, form):
        count += 1
        is_recent = bool(s["created_at"] and s["created_at"] >= since)
        recent += is_recent
        if not seen:
            if cursor and s["id"] == cursor:
                seen = True
            else:
                new.append(s)
        if seen and not is_recent:
            break
    total = int(form.get("submission_count") or 0) or count
    return list(reversed(new)), recent, total, _has_consent_field(form)


def metrics(inst: dict, company: dict | None) -> list[tuple[str, float, str]]:
    """The hourly poll: new submissions become contacts, then the two signup counts."""
    company = _company_of(inst, company)
    if company is None:
        return []
    cursor = _cursor(inst, company)
    if mode(inst) == "worker":
        new, total, recent = _worker_subs(inst, after=cursor)
        has_field = None
        label = "Worker"
    elif mode(inst) == "netlify":
        new, recent, total, has_field = _new_netlify(inst, company, cursor)
        label = "Netlify"
    else:
        raise FormsError(f"Mode must be netlify or worker, not '{mode(inst)}'")
    result = to_contacts(company, new, has_field)
    if new:
        _save_cursor(inst, company, new[-1]["id"])
    if result["added"]:
        from . import bus
        n = len(result["added"])
        bus.emit(company["id"], "brain", f"{n} new lead{'s' if n != 1 else ''} from the site's forms "
                 f"(contacts #{', #'.join(str(i) for i in result['added'][:10])})", None, {"connector": "forms"})
    skipped = [f"{result[k]} {w}" for k, w in (("known", "already known"), ("suppressed", "on the do-not-email list"),
                                               ("no_email", "without an email")) if result[k]]
    note = f"{label}; {len(new)} new this poll, {len(result['added'])} saved as leads"
    if skipped:
        note += " (" + ", ".join(skipped) + ")"
    return [("forms.signups", float(recent), "7d"), ("forms.signups_total", float(total), note)]


# ---------------------------------------------------------------- health and the tool

def health(inst: dict) -> tuple[str, str]:
    m = mode(inst)
    try:
        if m == "netlify":
            token, form = _netlify_form(inst, None, timeout=HEALTH_TIMEOUT)
            path = NETLIFY_SUBMISSIONS.format(form=urllib.parse.quote(str(form.get("id") or ""), safe=""))
            _netlify(inst, token, path, {"per_page": 1, "page": 1}, timeout=HEALTH_TIMEOUT)
            consent = "has a consent field" if _has_consent_field(form) else "no consent field (leads are flagged)"
            return "ok", (f"Netlify form {form.get('name')}: {int(form.get('submission_count') or 0)} submission(s); "
                          f"{consent}")
        if m == "worker":
            name = _form_name(inst)
            params = {"limit": 1, **({"form": name.lower()} if name else {})}
            answer = _worker(inst, params, timeout=HEALTH_TIMEOUT)
            return "ok", (f"Worker: {int(answer.get('total') or 0)} submission(s) stored"
                          + (f" for the {name} form" if name else ""))
    except FormsError as e:
        return "bad", str(e)[:300]
    return "bad", f"Mode must be netlify or worker, not '{m}'"


def recent_signups(inst: dict, company: dict | None, days: int = WINDOW_DAYS) -> str:
    from . import brain, verify   # imported here: brain imports tools, which imports the connector framework
    try:
        days = max(1, min(int(days or WINDOW_DAYS), 90))
    except (TypeError, ValueError):
        days = WINDOW_DAYS
    company = _company_of(inst, company)
    since = _now() - _dt.timedelta(days=days)
    has_field = None
    if mode(inst) == "worker":
        subs, _, _ = _worker_subs(inst, since=since)
        subs = list(reversed(subs))
    else:
        token, form = _netlify_form(inst, company)
        has_field = _has_consent_field(form)
        subs = []
        for s in _netlify_subs(inst, token, form):
            if s["created_at"] and s["created_at"] < since:
                break
            subs.append(s)
    subs = [s for s in subs if not s["created_at"] or s["created_at"] >= since]
    if not subs:
        return f"No signups in the last {days} day(s) ({mode(inst)} forms)."
    lines = [f"{len(subs)} signup(s) in the last {days} day(s), newest first ({mode(inst)} forms; addresses are in "
             "the contact list):"]
    for s in subs[:MAX_SHOWN]:
        data = s["data"]
        email = email_of(data)
        if not email:
            where = "no email given"
        elif verify.is_suppressed(email):
            where = "on the do-not-email list"
        else:
            c = brain._find_contact(company["id"], email) if company else None
            where = f"contact #{c['id']} ({c['stage']})" if c else "not in contacts yet (the next hourly poll adds it)"
        when = s["created_at"].strftime("%Y-%m-%d %H:%M UTC") if s["created_at"] else "date unknown"
        consent = consent_of(data, has_field)
        lines.append(f"- {when}, {s['form']} form: {name_of(data) or 'no name'}, consent "
                     f"{consent or 'not asked'}, {where}")
    if len(subs) > MAX_SHOWN:
        lines.append(f"- ... and {len(subs) - MAX_SHOWN} more")
    return "\n".join(lines)


TOOLS = [
    {"name": "recent_signups", "tag": "read", "fn": recent_signups, "external": True,
     "description": "Signups from the company's site forms in the last N days: when, which form, the name, consent "
                    "and the matching contact. Addresses stay in the contact list.",
     "parameters": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "How many days back (default 7, at most 90)"}}, "required": []}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


DEFINITION = {
    "label": "Site forms",
    "category": "Signups",
    "kind": "api",
    "scope": "company",
    "setup": {
        "where_to_get_key": (
            "Netlify mode: give the site's form data-netlify=\"true\" and a name (the site audit checks for it), "
            "deploy, and put that name here; the company's Netlify token and site are used. Worker mode: deploy "
            "docs/forms-worker.js as a Cloudflare Worker (its header says how), point the form's action at it, "
            "and put its address and shared secret here. A consent checkbox named consent records consent."),
        "permissions": ["Netlify: the company's Netlify token (already in Settings)",
                        "Worker: the shared secret you set on it"],
        "fields": [
            {"key": "mode", "label": "Mode", "secret": False, "hint": "netlify or worker", "placeholder": "netlify"},
            {"key": "form_name", "label": "Form name", "secret": False,
             "hint": "The form's name attribute (Netlify); with a Worker, only that form (blank: every form)",
             "placeholder": "signup"},
            {"key": "netlify_site_id", "label": "Netlify site id", "secret": False,
             "hint": "Blank: the site Holdco deploys to", "placeholder": ""},
            {"key": "endpoint_url", "label": "Worker address", "secret": False, "hint": "Worker mode only",
             "placeholder": "https://forms.your-name.workers.dev/"},
            {"key": "shared_secret", "label": "Shared secret", "secret": True,
             "hint": "Worker mode only: the SHARED_SECRET you set on the Worker", "placeholder": ""},
            {"key": "api_base", "label": "Netlify API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": NETLIFY_API},
        ],
        "test_mode": "Not applicable: it only reads. Submit your own form once and press Test.",
        "docs_url": "https://docs.netlify.com/forms/setup/",
        "agents_can": ["See real signups as a metric (forms.signups, polled hourly)",
                       "Find each signup in the contact list as a lead with its source and consent",
                       "Follow up through the normal outreach gate: the first message to a new contact always asks"],
    },
    "health": health,
    "health_timeout": HEALTH_TIMEOUT * 2 + 5,
    "metrics": metrics,
    "tools": tools,
}

# The definition this module provides, by key (connectors.DEFINITIONS merges it).
DEFINITIONS = {"forms": DEFINITION}
