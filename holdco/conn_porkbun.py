"""The porkbun connector (Phase 4, docs/phases/phase-4-scale.md section 3).

Domains through Porkbun's API v3 (https://api.porkbun.com/api/json/v3): every call is a POST with a JSON body, and
the keys travel in the X-API-Key and X-Secret-API-Key headers. Agents check a name's availability and price, list
the account's domains, read DNS records, set one (write: connector_write asks), and propose a purchase.

register_domain is the one connector call in Holdco that buys something when the owner approves it. It is tagged
spend, so it goes through the spend_money policy, which is locked: no policy and no trust level runs it on its own.
It is irreversible too (the request carries tags ["spend", "irreversible"]). Before it is queued, actions.gate asks
prepare_call for Porkbun's price now; the request card and the phone show that price and "This buys a domain and
cannot be undone", and a name that is not available is refused at once. At execution the price is checked again: a
higher price than the one approved buys nothing.

More guards:
- dry_run (a card field, on unless the owner unticks it): every write is sent with dryRun: true, so Porkbun answers
  what would happen and nothing is bought or changed.
- The mode follows the key (sandbox keys start with pk1_sb_) and is confirmed by the "sandbox": true flag Porkbun
  puts in its answers; a sandbox-looking key whose answers do not say sandbox counts as live.
- register_domain and set_dns_record send an Idempotency-Key built from the approval id and what the request asks
  for, so a retried approval (a lost reply, then a retry) is replayed by Porkbun instead of acting twice.

The endpoint paths are the PATH_* constants below. ping, domain/checkDomain, domain/listAll and the dns/ paths are
the documented API v3 paths; domain/create, the dryRun flag, the sandbox flag and the Idempotency-Key replay come
from the plan's description of Porkbun's agent features. Verify them all against Porkbun's API v3 documentation
before connecting.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets as _secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.porkbun.com/api/json/v3"
TIMEOUT = 30             # seconds for one API call
BUY_TIMEOUT = 60         # a registration may take longer
HEALTH_TIMEOUT = 15
SANDBOX_PREFIX = "pk1_sb_"
RETRY_WAIT = 10          # seconds to wait once when Porkbun limits domain checks (it allows about one per 10 s)

# Endpoint paths (verify against Porkbun's API v3 documentation before connecting).
PATH_PING = "/ping"                                          # authenticated ping: {"status", "yourIp"}
PATH_CHECK = "/domain/checkDomain/{domain}"                  # {"response": {"avail", "price", "additional"...}}
PATH_LIST = "/domain/listAll"                                # {"domains": [{"domain", "status", "expireDate"...}]}
PATH_DNS = "/dns/retrieve/{domain}"                          # {"records": [{"id", "name", "type", "content"...}]}
PATH_DNS_FIND = "/dns/retrieveByNameType/{domain}/{type}"    # + "/{subdomain}" unless it is the domain itself
PATH_DNS_CREATE = "/dns/create/{domain}"                     # {name, type, content, ttl, prio} -> {"id"}
PATH_DNS_EDIT = "/dns/editByNameType/{domain}/{type}"        # + "/{subdomain}"; {content, ttl, prio}
PATH_REGISTER = "/domain/create/{domain}"                    # {cost (cents), agreeToTerms} (the plan's; verify)

DNS_TYPES = ("A", "AAAA", "CNAME", "ALIAS", "MX", "TXT", "NS", "SRV", "CAA", "HTTPS", "SVCB", "TLSA")
MIN_TTL = 600            # Porkbun's smallest TTL
MAX_YEARS = 10
NOTICE = "This buys a domain and cannot be undone."
TAGS = ["spend", "irreversible"]
_TRUE = ("1", "true", "yes", "on", "y")
_KEY_RE = re.compile(r"\b(?:pk|sk)1_[A-Za-z0-9_]+")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # fakes on 127.0.0.1 skip any proxy
_proxied = urllib.request.build_opener()

# Whether Porkbun's last answer to an instance said sandbox, by (instance id, key fingerprint): a new key or a new
# instance with a reused id starts without an answer.
_seen: dict[tuple, bool] = {}
_seen_lock = threading.Lock()


class PorkbunError(RuntimeError):
    """A Porkbun call failed. `status` is the HTTP status (0 when Porkbun could not be reached)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- instance helpers

def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _keys(inst: dict | None) -> tuple[str, str]:
    if not inst:
        return "", ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    s = connectors.secrets(inst) or {}
    return str(s.get("api_key") or "").strip(), str(s.get("secret_key") or "").strip()


def _base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or API_BASE).rstrip("/")


def dry_run(inst: dict | None) -> bool:
    """On unless the card says otherwise: a card saved before the field existed, or without it, is a dry run."""
    v = _cfg(inst).get("dry_run")
    if v is None or (isinstance(v, str) and not v.strip()):
        return True
    return v is True or str(v).strip().lower() in _TRUE


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _note_mode(inst: dict, key: str, body) -> None:
    if not isinstance(body, dict):
        return
    flag = body.get("sandbox")
    with _seen_lock:
        _seen[(inst.get("id"), _fingerprint(key))] = flag is True or str(flag).strip().lower() == "true"


def forget_modes() -> None:
    """Forget every answer's sandbox flag (tests)."""
    with _seen_lock:
        _seen.clear()


def mode(inst: dict | None) -> str:
    """"sandbox" or "live": Porkbun's own flag in its last answer when there was one, else the key's prefix."""
    key = _keys(inst)[0]
    if not key:
        return "live"
    with _seen_lock:
        seen = _seen.get(((inst or {}).get("id"), _fingerprint(key)))
    if seen is not None:
        return "sandbox" if seen else "live"
    return "sandbox" if key.startswith(SANDBOX_PREFIX) else "live"


def test_mode(inst: dict | None) -> bool:
    """A sandbox key or dry run on shows "test mode" on the card."""
    return mode(inst) == "sandbox" or dry_run(inst)


def _scrub(text: str, *keys: str) -> str:
    text = str(text or "")
    for k in keys:
        if k and len(k) >= 6:
            text = text.replace(k, "[key]")
    return _KEY_RE.sub("[key]", text)


# ---------------------------------------------------------------- HTTP

def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request(inst: dict, path: str, body: dict | None = None, idempotency_key: str = "",
             timeout: float = TIMEOUT) -> dict:
    """One POST. Returns the parsed answer; an HTTP error or {"status": "ERROR"} raises PorkbunError."""
    key, secret = _keys(inst)
    if not key or not secret:
        raise PorkbunError("No Porkbun keys are set. Add the API key and the secret key on the Porkbun card in "
                           "Settings.")
    url = _base(inst) + path
    headers = {"X-API-Key": key, "X-Secret-API-Key": secret, "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": "Holdco"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:255]
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST", headers=headers)
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(200_000)
    except urllib.error.URLError as e:
        raise PorkbunError(_scrub(f"Cannot reach Porkbun: {e.reason}", key, secret)) from None
    except (TimeoutError, socket.timeout):
        raise PorkbunError("Porkbun did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise PorkbunError(_scrub(f"Cannot reach Porkbun: {e}", key, secret)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text or "{}")
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    if status >= 400 or str(parsed.get("status") or "").upper() == "ERROR":
        msg = str(parsed.get("message") or text[:200] or "no details").strip()
        raise PorkbunError(_scrub(f"Porkbun answered HTTP {status}: {msg}", key, secret), status)
    _note_mode(inst, key, parsed)
    return parsed


def _idempotency_key(inst: dict, approval_id, what: dict) -> str:
    """The approval's id plus what the request asks for: a retry of the same approval is the same request (Porkbun
    replays it within 24 h), while an edited request, or one sent after dry run was turned off, is a new one.
    Without an approval (an auto policy) the key is random: nothing to replay."""
    if not approval_id:
        return f"holdco-{_secrets.token_hex(16)}"
    digest = hashlib.sha256(json.dumps([inst.get("id"), what], sort_keys=True, default=str).encode()).hexdigest()
    return f"holdco-approval-{int(approval_id)}-{digest[:24]}"


# ---------------------------------------------------------------- values

def _domain(value) -> str:
    """A domain name as Porkbun wants it: lower case, no scheme, path or trailing dot, IDNA for other scripts."""
    d = str(value or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = re.split(r"[/?#]", d, maxsplit=1)[0].rstrip(".")
    if "@" in d:
        d = d.rsplit("@", 1)[1]
    try:
        d = d.encode("idna").decode("ascii") if d else d
    except UnicodeError:
        raise ValueError(f"'{value}' is not a domain name") from None
    labels = d.split(".")
    if len(labels) < 2 or not all(_LABEL.match(x) for x in labels) or labels[-1].isdigit():
        raise ValueError(f"'{value}' is not a domain name; give one like pawplan.co")
    return d


def _price(v) -> float | None:
    try:
        p = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return p if p >= 0 else None


def _years(v) -> int:
    try:
        n = int(float(v if v not in (None, "") else 1))
    except (TypeError, ValueError):
        raise ValueError("years must be a whole number from 1 to 10") from None
    if not 1 <= n <= MAX_YEARS:
        raise ValueError("years must be a whole number from 1 to 10")
    return n


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _subdomain(name, domain: str) -> str:
    """The record's name relative to the domain: "" for the domain itself (also "@" or the full domain)."""
    n = str(name or "").strip().lower().rstrip(".")
    if n in ("", "@", domain):
        return ""
    if n.endswith("." + domain):
        n = n[: -len(domain) - 1]
    if not all(x == "*" or _LABEL.match(x) or re.fullmatch(r"_[a-z0-9-]+", x) for x in n.split(".")):
        raise ValueError(f"'{name}' is not a record name; use www, @ for the domain itself, or _dmarc")
    return n


def _fqdn(sub: str, domain: str) -> str:
    return f"{sub}.{domain}" if sub else domain


# ---------------------------------------------------------------- availability and price

def _check(inst: dict, domain: str, wait: bool = False) -> dict:
    """Porkbun's answer for one name. wait=True waits once when Porkbun limits domain checks (it allows about one
    every 10 seconds), for the paths an agent cannot simply retry: queueing a purchase and running it."""
    path = PATH_CHECK.format(domain=urllib.parse.quote(domain, safe=""))
    try:
        body = _request(inst, path)
    except PorkbunError as e:
        if not wait or not (e.status == 429 or "limit" in str(e).lower()):
            raise
        time.sleep(RETRY_WAIT)
        body = _request(inst, path)
    r = body.get("response")
    if not isinstance(r, dict):
        raise PorkbunError("Porkbun's answer had no availability for this name.")
    return r


def quote(inst: dict, domain: str, years: int = 1, wait: bool = False) -> dict:
    """{domain, available, first_year, renewal, total, premium, promo} for registering domain for `years`: the first
    year at Porkbun's price now, the others at its renewal price. Prices are None when Porkbun gave none."""
    r = _check(inst, domain, wait=wait)
    first = _price(r.get("price"))
    renewal = _price(((r.get("additional") or {}).get("renewal") or {}).get("price"))
    if renewal is None:
        renewal = _price(r.get("regularPrice")) if r.get("regularPrice") is not None else first
    total = None
    if first is not None and (years == 1 or renewal is not None):
        total = round(first + (renewal or 0) * (years - 1), 2)
    return {"domain": domain, "available": str(r.get("avail") or "").strip().lower() in ("yes", "true", "1"),
            "first_year": first, "renewal": renewal, "total": total,
            "premium": str(r.get("premium") or "").strip().lower() in ("yes", "true", "1"),
            "promo": str(r.get("firstYearPromo") or "").strip().lower() in ("yes", "true", "1")}


# ---------------------------------------------------------------- tools

def check_domain(inst: dict, company: dict | None, domain: str) -> str:
    d = _domain(domain)
    q = quote(inst, d)
    m = mode(inst)
    if not q["available"]:
        return f"{d} is not available to register ({m} mode)."
    if q["first_year"] is None:
        return f"{d} is available, but Porkbun gave no price ({m} mode)."
    extra = []
    if q["promo"]:
        extra.append("first-year promotion")
    if q["premium"]:
        extra.append("premium name")
    renew = f", then {_usd(q['renewal'])} a year" if q["renewal"] is not None else ""
    return (f"{d} is available: {_usd(q['first_year'])} for the first year{renew}"
            f"{' (' + ', '.join(extra) + ')' if extra else ''} ({m} mode). Buying it needs the owner's approval "
            "(porkbun__register_domain).")


def list_domains(inst: dict, company: dict | None) -> str:
    body = _request(inst, PATH_LIST, {"start": "0", "includeLabels": "no"})
    domains = [x for x in (body.get("domains") or []) if isinstance(x, dict)]
    if not domains:
        return f"No domains in this Porkbun account ({mode(inst)} mode)."
    lines = [f"{len(domains)} domain(s) in the Porkbun account ({mode(inst)} mode):"]
    for x in domains[:200]:
        renew = "auto-renew on" if str(x.get("autoRenew")).strip() in ("1", "true", "True") else "auto-renew off"
        lines.append(f"- {x.get('domain')}: {str(x.get('status') or '?').lower()}, expires "
                     f"{str(x.get('expireDate') or '?')[:10]}, {renew}")
    if len(domains) > 200:
        lines.append(f"- ... and {len(domains) - 200} more")
    return "\n".join(lines)


def _record_line(r: dict) -> str:
    prio = f", priority {r.get('prio')}" if str(r.get("prio") or "0") not in ("0", "") else ""
    return (f"- {r.get('type')} {r.get('name')} -> {r.get('content')} (ttl {r.get('ttl') or '?'}{prio}, "
            f"id {r.get('id')})")


def dns_records(inst: dict, company: dict | None, domain: str) -> str:
    d = _domain(domain)
    body = _request(inst, PATH_DNS.format(domain=urllib.parse.quote(d, safe="")))
    records = [r for r in (body.get("records") or []) if isinstance(r, dict)]
    if not records:
        return f"No DNS records for {d} ({mode(inst)} mode)."
    return "\n".join([f"{len(records)} DNS record(s) for {d} ({mode(inst)} mode):"]
                     + [_record_line(r) for r in records[:300]])


def _by_name_type(path: str, domain: str, typ: str, sub: str) -> str:
    out = path.format(domain=urllib.parse.quote(domain, safe=""), type=typ)
    return out + (f"/{urllib.parse.quote(sub, safe='')}" if sub else "")


def set_dns_record(inst: dict, company: dict | None, domain: str, type: str, name: str, content: str,
                   ttl: int = MIN_TTL, approval_id: int | None = None) -> str:
    """Create the record, or replace the content of the records with this name and type. A record that already says
    exactly this is left alone, so a retried approval changes nothing twice."""
    d = _domain(domain)
    typ = str(type or "").strip().upper()
    if typ not in DNS_TYPES:
        raise ValueError(f"type must be one of {', '.join(DNS_TYPES)}")
    sub = _subdomain(name, d)
    content = str(content or "").strip()
    if not content:
        raise ValueError("content is required: the record's value, e.g. an IP address or a host name")
    try:
        ttl = max(MIN_TTL, int(float(ttl if ttl not in (None, "") else MIN_TTL)))
    except (TypeError, ValueError):
        raise ValueError("ttl must be a number of seconds (600 or more)") from None
    prio = None
    if typ in ("MX", "SRV"):
        m = re.match(r"^(\d{1,5})\s+(\S.*)$", content)
        if m:
            prio, content = m.group(1), m.group(2)
    fqdn = _fqdn(sub, d)
    found = _request(inst, _by_name_type(PATH_DNS_FIND, d, typ, sub)).get("records") or []
    found = [r for r in found if isinstance(r, dict)]
    if found and all(str(r.get("content")) == content and str(r.get("ttl")) == str(ttl) for r in found):
        return f"The {typ} record {fqdn} already points to {content} (ttl {ttl}); nothing changed ({mode(inst)} mode)."
    dry = dry_run(inst)
    body: dict = {"content": content, "ttl": str(ttl)}
    if prio is not None:
        body["prio"] = prio
    if found:
        path, verb = _by_name_type(PATH_DNS_EDIT, d, typ, sub), "update"
    else:
        path, verb = PATH_DNS_CREATE.format(domain=urllib.parse.quote(d, safe="")), "create"
        body = {"name": sub, "type": typ, **body}
    if dry:
        body["dryRun"] = True
    key = _idempotency_key(inst, approval_id, {"tool": "set_dns_record", "domain": d, "type": typ, "name": sub,
                                               "content": content, "ttl": ttl, "prio": prio, "dry_run": dry})
    r = _request(inst, path, body, idempotency_key=key)
    what = f"the {typ} record {fqdn} -> {content} (ttl {ttl})"
    if dry:
        return (f"Dry run (nothing changed): Porkbun would {verb} {what}{_porkbun_says(r)}. Untick Dry run on the "
                f"Porkbun card to change DNS for real ({mode(inst)} mode).")
    rid = f", id {r.get('id')}" if r.get("id") else ""
    return f"{'Created' if verb == 'create' else 'Updated'} {what}{rid} ({mode(inst)} mode)."


def _porkbun_says(r: dict) -> str:
    """The would-be result a dry run returned, in one line, without the plumbing."""
    rest = {k: v for k, v in (r or {}).items() if k not in ("status", "sandbox", "dryRun")}
    if not rest:
        return ""
    return f". Porkbun answered: {json.dumps(rest, sort_keys=True, default=str)[:400]}"


def prepare_call(inst: dict, company: dict | None, tool: str, args: dict) -> dict | None:
    """actions.gate asks this before a Porkbun call is queued or run. register_domain gets Porkbun's price now (the
    owner approves that price, and the card and the phone show it with the warning); a name that is not available,
    or has no price, is refused with the reason. Other tools need nothing."""
    if tool != "register_domain":
        return None
    d = _domain(args.get("domain"))
    years = _years(args.get("years"))
    q = quote(inst, d, years, wait=True)
    if not q["available"]:
        raise ValueError(f"{d} is not available to register. Check other names with porkbun__check_domain.")
    if q["total"] is None:
        raise ValueError(f"Porkbun gave no price for {d}, so Holdco cannot ask the owner to buy it.")
    total, dry, m = q["total"], dry_run(inst), mode(inst)
    span = f"{years} year{'s' if years != 1 else ''}"
    notice = f"Buys {d} for {_usd(total)} ({span}) at Porkbun. {NOTICE}"
    if q["premium"]:
        notice += " It is a premium name."
    if dry:
        notice += " Dry run is on: approving only shows what Porkbun would do; nothing is bought."
    elif m == "sandbox":
        notice += " Sandbox keys: Porkbun's sandbox, no real purchase."
    return {"args": {"domain": d, "years": years, "price_usd": total}, "amount_usd": total, "dry_run": dry,
            "mode": m, "tags": list(TAGS), "notice": notice, "_why_asked": notice}


def _request_card(approval_id) -> dict:
    """The approved request's payload: prepare_call's dry_run and mode as they were when the owner was asked."""
    from . import db   # imported here: only a purchase needs it
    row = db.one("SELECT payload FROM approvals WHERE id=?", (int(approval_id),))
    p = db.loads(row["payload"], {}) if row else {}
    return p if isinstance(p, dict) else {}


def register_domain(inst: dict, company: dict | None, domain: str, years: int = 1, price_usd=None,
                    approval_id: int | None = None) -> str:
    """Buy the domain at the price the owner approved (price_usd, set from Porkbun's quote when it was queued) or
    less. Runs only from an approved request: the approval's id keys the request, so a retry is replayed by Porkbun
    instead of buying twice."""
    if not approval_id:
        raise RuntimeError("A domain purchase runs only from a request the owner approved.")
    d = _domain(domain)
    years = _years(years)
    approved = _price(price_usd)
    if approved is None:
        raise RuntimeError("This request has no approved price, so nothing was bought; ask for it again.")
    asked = _request_card(approval_id)   # what the request card and the phone told the owner
    # A request queued while dry run was on said "nothing is bought": approving it stays a dry run, even when the
    # card's Dry run was unticked in the meantime.
    dry = dry_run(inst) or asked.get("dry_run") is True
    q = quote(inst, d, years, wait=True)
    if not dry and asked.get("mode") == "sandbox" and mode(inst) == "live":
        raise RuntimeError(f"{d} was requested with Porkbun's sandbox keys (no real purchase), but the keys are live "
                           "now, so nothing was bought. Ask again to approve a real purchase.")
    span = f"{years} year{'s' if years != 1 else ''}"
    if q["available"]:
        live = q["total"]
        if live is None:
            raise RuntimeError(f"Porkbun gave no price for {d} now, so nothing was bought; ask for it again.")
        if live > approved + 0.005:
            raise RuntimeError(f"The price of {d} went up from {_usd(approved)} to {_usd(live)} since this was "
                               "approved, so nothing was bought. Ask again to approve the new price.")
        cost = live
    else:
        # Not available any more: either someone else has it (Porkbun refuses below) or an earlier attempt of this
        # very request bought it and its reply was lost; the same Idempotency-Key then gets the first answer back.
        cost = approved
    body: dict = {"cost": int(round(cost * 100)), "agreeToTerms": "yes"}
    if years != 1:
        body["years"] = years   # verify against the docs: whether domain/create takes a registration period
    if dry:
        body["dryRun"] = True
    key = _idempotency_key(inst, approval_id, {"tool": "register_domain", "domain": d, "years": years,
                                               "dry_run": dry})
    try:
        r = _request(inst, PATH_REGISTER.format(domain=urllib.parse.quote(d, safe="")), body, idempotency_key=key,
                     timeout=BUY_TIMEOUT)
    except PorkbunError as e:
        if not q["available"] and e.status != 0:
            raise PorkbunError(f"{d} is not available any more; nothing was bought. ({e})", e.status) from None
        raise
    m = mode(inst)
    if dry or r.get("dryRun") is True:
        return (f"Dry run (nothing bought): Porkbun would register {d} for {_usd(cost)} ({span}){_porkbun_says(r)}. "
                f"Untick Dry run on the Porkbun card to buy it for real, then ask again ({m} mode).")
    cents = _price(r.get("cost"))   # Porkbun answers the cost in cents, like the request
    paid = cents / 100 if cents is not None else cost
    order = f", order {r.get('orderId')}" if r.get("orderId") else ""
    renew = f" It renews at {_usd(q['renewal'])} a year unless auto-renew is turned off at Porkbun." \
        if q["renewal"] is not None else ""
    return f"Registered {d} for {_usd(paid)} ({span}{order}, {m} mode). It cannot be undone.{renew}"


def _string(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS = [
    {"name": "check_domain", "tag": "read", "fn": check_domain,
     "description": "Check whether a domain name is available at Porkbun and what it costs (first year and "
                    "renewal). Use it while naming and branding.",
     "parameters": {"type": "object", "properties": {"domain": _string("The domain, e.g. pawplan.co")},
                    "required": ["domain"]}},
    {"name": "list_domains", "tag": "read", "fn": list_domains,
     "description": "List the domains in the owner's Porkbun account: status, expiry date and auto-renew.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "dns_records", "tag": "read", "fn": dns_records,
     "description": "Read the DNS records of a domain in the Porkbun account (type, name, value, ttl).",
     "parameters": {"type": "object", "properties": {"domain": _string("The domain, e.g. pawplan.co")},
                    "required": ["domain"]}},
    {"name": "set_dns_record", "tag": "write", "fn": set_dns_record, "takes_approval_id": True,
     "description": "Create a DNS record at Porkbun, or replace the value of the records with that name and type "
                    "(for the site, email verification or a mail server). Waits for the owner's approval unless "
                    "they allow changes.",
     "parameters": {"type": "object", "properties": {
         "domain": _string("The domain, e.g. pawplan.co"),
         "type": _string("Record type: A, AAAA, CNAME, ALIAS, MX, TXT, NS, SRV, CAA, HTTPS, SVCB or TLSA"),
         "name": _string("The record's name: www, @ for the domain itself, or _dmarc"),
         "content": _string("The value: an IP address, a host name or text (MX: '10 mail.example.com')"),
         "ttl": {"type": "integer", "description": "Seconds, 600 or more (default 600)"}},
         "required": ["domain", "type", "name", "content"]}},
    {"name": "register_domain", "tag": "spend", "tags": list(TAGS), "fn": register_domain, "takes_approval_id": True,
     "description": "Propose buying a domain at Porkbun. Spend and irreversible: it always waits for the owner, "
                    "whose request card shows Porkbun's price. " + NOTICE,
     "parameters": {"type": "object", "properties": {
         "domain": _string("The domain to buy, e.g. pawplan.co (check it first with porkbun__check_domain)"),
         "years": {"type": "integer", "description": "How many years (default 1)", "minimum": 1,
                   "maximum": MAX_YEARS}},
         "required": ["domain"]}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


# ---------------------------------------------------------------- health

def health(inst: dict) -> tuple[str, str]:
    key, secret = _keys(inst)
    if not key or not secret:
        return "bad", "No keys: paste the API key (pk1_...) and the secret key (sk1_...)"
    try:
        _request(inst, PATH_PING, timeout=HEALTH_TIMEOUT)
    except PorkbunError as e:
        if e.status in (401, 403) or "api key" in str(e).lower():
            return "bad", "keys rejected"
        return "bad", str(e)[:300]
    m = mode(inst)
    run = ("dry run on: nothing is bought or changed" if dry_run(inst)
           else "dry run off: approved purchases and DNS changes are real")
    if key.startswith(SANDBOX_PREFIX) and m != "sandbox":
        return "warn", (f"The key starts with {SANDBOX_PREFIX} but Porkbun's answer does not say sandbox, so Holdco "
                        f"treats it as live; {run}")
    return "ok", f"{m} mode, {run}"


DEFINITION = {
    "label": "Porkbun",
    "category": "Domains",
    "kind": "api",
    "scope": "global",          # API keys are account-level; a purchase is attributed to the company that asked
    "setup": {
        "where_to_get_key": (
            "In your Porkbun account open API Access, create an API key and copy both the API key (pk1_...) and "
            "the secret key (sk1_...). Start with sandbox keys (pk1_sb_...): Porkbun's sandbox never buys "
            "anything. For DNS changes, turn on API access for each domain Holdco may manage (the domain's "
            "Details in Domain Management)."),
        "permissions": [
            "Create the key without purchase rights unless you intend to buy domains through Holdco",
            "Domain and DNS access for the domains agents may manage",
            "Porkbun's own default agent spend cap is $100 a month: keep it, or set it lower",
        ],
        "fields": [
            {"key": "api_key", "label": "API key", "secret": True, "hint": "Starts with pk1_ (sandbox: pk1_sb_)",
             "placeholder": "pk1_sb_..."},
            {"key": "secret_key", "label": "Secret API key", "secret": True, "hint": "Starts with sk1_",
             "placeholder": "sk1_..."},
            {"key": "dry_run", "label": "Dry run", "secret": False, "type": "bool", "default": True,
             "hint": "On: purchases and DNS changes are sent with dryRun, so Porkbun only says what would happen. "
                     "Untick it when you want approved purchases to buy for real.",
             "placeholder": "yes"},
            {"key": "api_base", "label": "API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": API_BASE},
        ],
        "test_mode": (
            "A sandbox key (pk1_sb_...) or Dry run shows test mode here. The mode follows the key and Porkbun's own "
            "sandbox flag in its answers. Dry run is on until you untick it. Buying a domain always waits for you, "
            "whatever the policies and the trust ladder say, and the request shows the price."),
        "docs_url": "https://porkbun.com/api/json/v3/documentation",
        "agents_can": ["Check whether names are available and what they cost, while naming and branding",
                       "List the account's domains and read their DNS records",
                       "Set DNS records for the site and the email domain (asks you unless you allow changes)",
                       "Propose a domain purchase: it always asks you, with the price; it cannot be undone"],
    },
    "health": health,
    "tools": tools,
    "test_mode": test_mode,
    "prepare_call": prepare_call,   # actions.gate: the price and the warning on register_domain's request card
}

DEFINITIONS = {"porkbun": DEFINITION}
