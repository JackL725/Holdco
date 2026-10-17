"""The stripe connector (Phase 1, docs/phases/phase-1-connect.md section 7).

Typed REST tools against Stripe's API (form-encoded bodies, Bearer auth, bracket syntax for nested parameters):
create and inspect payment links, read recent payments, refund (always asks), and the metrics Holdco polls on its
own (revenue, payments, customers, MRR) so nobody has to type them in.

Stripe's hosted MCP server (https://mcp.stripe.com) is a second, optional definition, `stripe_mcp` (kind
mcp-http), for what the typed tools do not cover. It only does anything when the Stripe card has `use_mcp`
ticked; it reuses the Stripe card's key unless it has its own. The typed tools stay the primary path.

The key never leaves this module except in the Authorization header: error texts are scrubbed of anything that
looks like a Stripe key before they reach a result, an event or a log.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stripe.com"
MCP_URL = "https://mcp.stripe.com"
TIMEOUT = 30            # seconds for one API call
HEALTH_TIMEOUT = 15     # the framework's health budget
PAGE_CAP = 10000        # most objects one metric will page through
INTERVALS = ("month", "year", "week", "day")
_INTERVAL_WORDS = {"monthly": "month", "months": "month", "mo": "month", "yearly": "year", "years": "year",
                   "annual": "year", "annually": "year", "weekly": "week", "weeks": "week", "daily": "day",
                   "days": "day", "once": "", "one-time": "", "onetime": "", "none": ""}
# Currencies Stripe counts in whole units (no cents).
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf",
                "xof", "xpf"}
# Currencies Stripe counts in thousandths, with their own rounding rules: Holdco does not handle them.
THREE_DECIMAL = {"bhd", "jod", "kwd", "omr", "tnd"}
# Anything shaped like a Stripe key (Stripe itself echoes a masked key in some errors).
_KEY_RE = re.compile(r"\b(?:sk|rk|pk)_(?:test|live)_[A-Za-z0-9*]+")
_TRUE = ("1", "true", "yes", "on", "y")

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # fakes on 127.0.0.1 skip any proxy
_proxied = urllib.request.build_opener()


class StripeError(RuntimeError):
    """A Stripe call failed. `status` is the HTTP status (0 when Stripe could not be reached)."""

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


def _key(inst: dict | None) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get("api_key") or "").strip()


def _base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or API_BASE).rstrip("/")


def mode_of_key(key: str) -> str:
    """Stripe test keys carry "_test_" after the type prefix (sk_test_, rk_test_); anything else is live."""
    return "test" if re.match(r"^(?:sk|rk)_test_", key or "") else "live"


def mode(inst: dict | None) -> str:
    return mode_of_key(_key(inst))


def test_mode(inst: dict | None) -> bool:
    return mode(inst) == "test"


def use_mcp(inst: dict | None) -> bool:
    v = _cfg(inst).get("use_mcp")
    return v is True or str(v or "").strip().lower() in _TRUE


def _scrub(text: str, key: str = "") -> str:
    text = str(text or "")
    if key and len(key) >= 6:
        text = text.replace(key, "[key]")
    return _KEY_RE.sub("[key]", text)


# ---------------------------------------------------------------- HTTP

def _flatten(value, prefix: str, out: list) -> None:
    """Stripe's bracket syntax: {"recurring": {"interval": "month"}} -> recurring[interval]=month,
    {"line_items": [{"price": "p"}]} -> line_items[0][price]=p, {"expand": ["x"]} -> expand[]=x."""
    if value is None:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{prefix}[{k}]" if prefix else str(k), out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            if isinstance(v, (dict, list, tuple)):
                _flatten(v, f"{prefix}[{i}]", out)
            else:
                _flatten(v, f"{prefix}[]", out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    else:
        out.append((prefix, str(value)))


def encode(params: dict | None) -> str:
    out: list = []
    _flatten(params or {}, "", out)
    return urllib.parse.urlencode(out)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request(inst: dict, method: str, path: str, params: dict | None = None, timeout: float = TIMEOUT,
             idempotent: bool = False, approval_id: int | None = None) -> dict:
    key = _key(inst)
    if not key:
        raise StripeError("No Stripe key is set. Add one on the Stripe card in the company's Settings.")
    url = _base(inst) + path
    query = encode(params)
    data = None
    if method == "GET":
        if query:
            url += ("&" if "?" in url else "?") + query
    else:
        data = query.encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": "Holdco"}
    if idempotent and method == "POST":
        # A retried approval (a lost reply, then a retry) must not refund twice. Stripe keeps an idempotency key
        # for 24 hours and answers a repeat with the first result. The key is the approval's id plus the request,
        # so a retry of the same approval replays while two approvals for the same amount are two refunds (an edited
        # retry is a new request too); without an approval the key is random. Creating links is not keyed: a
        # repeat there only adds a duplicate, while a replayed answer could hand back a link turned off since.
        if approval_id:
            digest = hashlib.sha256(f"{inst.get('id')}|{path}|{query}".encode()).hexdigest()[:24]
            headers["Idempotency-Key"] = f"holdco-approval-{int(approval_id)}-{digest}"
        else:
            headers["Idempotency-Key"] = f"holdco-{secrets.token_hex(16)}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(200_000)
    except urllib.error.URLError as e:
        raise StripeError(_scrub(f"Cannot reach Stripe: {e.reason}", key)) from None
    except (TimeoutError, socket.timeout):
        raise StripeError("Stripe did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise StripeError(_scrub(f"Cannot reach Stripe: {e}", key)) from None
    try:
        body = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        body = {}
    if status >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        msg = (err or {}).get("message") if isinstance(err, dict) else ""
        msg = msg or raw.decode("utf-8", "replace")[:200]
        raise StripeError(_scrub(f"Stripe answered HTTP {status}: {msg}", key), status)
    return body if isinstance(body, dict) else {}


def _list_all(inst: dict, path: str, params: dict | None = None, cap: int = PAGE_CAP) -> tuple[list, bool]:
    """Every object of a list endpoint (100 per page), up to cap. Returns (items, capped)."""
    params = {**(params or {}), "limit": 100}
    items: list = []
    while True:
        page = _request(inst, "GET", path, params)
        data = page.get("data") or []
        items.extend(data)
        if len(items) >= cap:
            return items[:cap], True
        if not page.get("has_more") or not data:
            return items, False
        params["starting_after"] = data[-1].get("id")


# ---------------------------------------------------------------- money and time

def _to_minor(amount: float, currency: str) -> int:
    """An amount in the currency's main unit as Stripe's integer: whole units for zero-decimal currencies (JPY),
    cents for the rest. Three-decimal currencies (KWD, BHD, ...) are refused rather than sent ten times too small."""
    cur = (currency or "").lower()
    if cur in THREE_DECIMAL:
        raise ValueError(f"{cur.upper()} uses three decimals, which Holdco does not handle; do this one in the "
                         "Stripe dashboard")
    return int(round(amount)) if cur in ZERO_DECIMAL else int(round(amount * 100))


def _money(minor, currency: str = "usd") -> str:
    cur = (currency or "usd").lower()
    try:
        minor = int(minor or 0)
    except (TypeError, ValueError):
        minor = 0
    if cur in ZERO_DECIMAL:
        return f"{minor} {cur.upper()}"
    value = f"{minor / 100:,.2f}"
    return f"${value}" if cur == "usd" else f"{value} {cur.upper()}"


def _when(epoch) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _interval(value: str) -> str:
    v = (value or "").strip().lower()
    v = _INTERVAL_WORDS.get(v, v)
    if v and v not in INTERVALS:
        raise ValueError("recurring_interval must be month, year, week or day (or empty for a one-time price)")
    return v


def _monthly(unit_amount: int, quantity: int, interval: str, count: int) -> float:
    """One subscription item's amount per month, in minor units: year/12, week*52/12, day*365/12."""
    per = unit_amount * max(1, quantity)
    count = max(1, count or 1)
    factor = {"month": 1.0, "year": 1 / 12, "week": 52 / 12, "day": 365 / 12}.get(interval, 0.0)
    return per * factor / count


# ---------------------------------------------------------------- tools

def create_payment_link(inst: dict, company: dict, name: str, amount_usd: float, currency: str = "usd",
                        description: str = "", recurring_interval: str = "") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required: the product name buyers see, e.g. 'Starter plan'")
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        raise ValueError("amount_usd must be a number, e.g. 19 or 19.99") from None
    if amount <= 0:
        raise ValueError("amount_usd must be more than 0")
    cur = (currency or "usd").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", cur):
        raise ValueError("currency is a three-letter code such as usd")
    interval = _interval(recurring_interval)
    unit = _to_minor(amount, cur)
    product_params = {"name": name}
    if (description or "").strip():
        product_params["description"] = description.strip()   # Stripe refuses an empty description
    product = _request(inst, "POST", "/v1/products", product_params)
    price_params = {"product": product.get("id"), "unit_amount": unit, "currency": cur}
    if interval:
        price_params["recurring"] = {"interval": interval}
    price = _request(inst, "POST", "/v1/prices", price_params)
    link = _request(inst, "POST", "/v1/payment_links",
                    {"line_items": [{"price": price.get("id"), "quantity": 1}]})
    per = f"/{interval}" if interval else ""
    return (f"Created payment link {link.get('url')} (id {link.get('id')}, {name}, {_money(unit, cur)}{per}, "
            f"{mode(inst)} mode)")


def list_payment_links(inst: dict, company: dict) -> str:
    page = _request(inst, "GET", "/v1/payment_links", {"limit": 20})
    links = page.get("data") or []
    if not links:
        return f"No payment links yet ({mode(inst)} mode)."
    lines = [f"{len(links)} payment link(s), newest first ({mode(inst)} mode):"]
    for link in links:
        lines.append(f"- {link.get('id')} {link.get('url')} active: {str(bool(link.get('active'))).lower()}, "
                     f"created {_when(link.get('created'))}")
    return "\n".join(lines)


def _product_name(inst: dict, item: dict) -> str:
    if item.get("description"):
        return str(item["description"])
    product = (item.get("price") or {}).get("product")
    if isinstance(product, dict):
        return str(product.get("name") or product.get("id") or "?")
    if product:
        try:
            return str(_request(inst, "GET", f"/v1/products/{urllib.parse.quote(str(product), safe='')}")
                       .get("name") or product)
        except StripeError:
            return str(product)
    return "?"


def _find_link_by_url(inst: dict, url: str) -> dict | None:
    want = url.strip().rstrip("/")
    links, _ = _list_all(inst, "/v1/payment_links", cap=300)
    return next((x for x in links if str(x.get("url") or "").rstrip("/") == want), None)


def find_payment_link(inst: dict, ref: str) -> dict | None:
    """Phase 3: a payment link retrieved from the Stripe API by id (plink_...) or by its URL (looked up to its id
    first), or None when this account has no such link. The verification gate (verify.py) uses it to see that a
    link someone handed off exists and is active. Other API errors raise StripeError."""
    ref = (ref or "").strip()
    if ref.lower().startswith(("http://", "https://")):
        found = _find_link_by_url(inst, ref)
        if not found:
            return None
        ref = str(found.get("id") or "")
    if not ref:
        return None
    try:
        return _request(inst, "GET", f"/v1/payment_links/{urllib.parse.quote(ref, safe='')}")
    except StripeError as e:
        if e.status == 404:
            return None
        raise


def get_payment_link(inst: dict, company: dict, id: str) -> str:
    ref = (id or "").strip()
    if not ref:
        raise ValueError("id is required: a payment link id (plink_...) or its URL")
    if ref.lower().startswith(("http://", "https://")):
        found = _find_link_by_url(inst, ref)
        if not found:
            return f"No payment link with the URL {ref} in this Stripe account ({mode(inst)} mode)."
        ref = str(found.get("id"))
    link = _request(inst, "GET", f"/v1/payment_links/{urllib.parse.quote(ref, safe='')}",
                    {"expand": ["line_items"]})
    items = ((link.get("line_items") or {}).get("data")) if isinstance(link.get("line_items"), dict) else None
    if items is None:
        items = _request(inst, "GET", f"/v1/payment_links/{urllib.parse.quote(ref, safe='')}/line_items"
                         ).get("data") or []
    lines = [f"id: {link.get('id')}", f"url: {link.get('url')}",
             f"active: {str(bool(link.get('active'))).lower()}"]
    for item in items:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        per = f"/{recurring.get('interval')}" if recurring.get("interval") else ""
        amount = price.get("unit_amount")
        if amount is None:
            amount = item.get("amount_total")
        lines.append(f"item: {_product_name(inst, item)}, {_money(amount, price.get('currency') or 'usd')}{per}, "
                     f"quantity {item.get('quantity') or 1}")
    lines.append(f"mode: {mode(inst)}")
    return "\n".join(lines)


def _charge_email(c: dict) -> str:
    return str((c.get("billing_details") or {}).get("email") or c.get("receipt_email") or "")


def recent_payments(inst: dict, company: dict, days: int = 7) -> str:
    try:
        days = max(1, min(int(days or 7), 90))
    except (TypeError, ValueError):
        days = 7
    since = int(time.time()) - days * 86400
    charges, capped = _list_all(inst, "/v1/charges", {"created": {"gte": since}}, cap=500)
    paid = [c for c in charges if c.get("status") == "succeeded" and c.get("paid", True)]
    if not paid:
        return f"No successful payments in the last {days} day(s) ({mode(inst)} mode)."
    usd = sum(int(c.get("amount") or 0) - int(c.get("amount_refunded") or 0)
              for c in paid if (c.get("currency") or "").lower() == "usd")
    lines = [f"{len(paid)} successful payment(s) in the last {days} day(s), {_money(usd)} USD after refunds "
             f"({mode(inst)} mode){' (first 500 only)' if capped else ''}:"]
    for c in paid[:50]:
        cur = c.get("currency") or "usd"
        refunded = int(c.get("amount_refunded") or 0)
        extra = f", refunded {_money(refunded, cur)}" if refunded else ""
        who = _charge_email(c) or "no email"
        lines.append(f"- {_when(c.get('created'))} {_money(c.get('amount'), cur)} {cur.upper()} from {who} "
                     f"(payment {c.get('payment_intent') or c.get('id')}{extra})")
    if len(paid) > 50:
        lines.append(f"- ... and {len(paid) - 50} more")
    return "\n".join(lines)


def _payment_currency(inst: dict, ref: str, is_charge: bool) -> str:
    """The currency of the payment a refund is for, looked up in Stripe (lower case)."""
    kind = "charges" if is_charge else "payment_intents"
    obj = _request(inst, "GET", f"/v1/{kind}/{urllib.parse.quote(ref, safe='')}")
    cur = str(obj.get("currency") or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", cur):
        raise StripeError(f"Stripe did not say which currency {ref} was paid in.")
    return cur


def create_refund(inst: dict, company: dict, payment_intent: str, amount_usd: float,
                  approval_id: int | None = None) -> str:
    """Refund part or all of a payment. The amount is in the payment's own currency (looked up first), and the
    approval's id keys the request so a retried approval cannot refund twice (connectors.execute passes it)."""
    ref = (payment_intent or "").strip()
    if not ref:
        raise ValueError("payment_intent is required (pi_..., or a charge id ch_...)")
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        raise ValueError("amount_usd must be a number") from None
    if amount <= 0:
        raise ValueError("amount_usd must be more than 0")
    is_charge = ref.startswith(("ch_", "py_"))
    cur = _payment_currency(inst, ref, is_charge)
    params = {"charge" if is_charge else "payment_intent": ref, "amount": _to_minor(amount, cur)}
    r = _request(inst, "POST", "/v1/refunds", params, idempotent=True, approval_id=approval_id)
    return (f"Refunded {_money(r.get('amount'), r.get('currency') or cur)} on {ref} (refund {r.get('id')}, "
            f"status {r.get('status') or 'pending'}, {mode(inst)} mode)")


def _number(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _string(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS = [
    {"name": "create_payment_link", "tag": "publish", "fn": create_payment_link,
     "description": "Create a Stripe payment link: a product, its price (one-time, or recurring monthly, yearly, "
                    "weekly or daily) and a link buyers can pay at. Returns the URL.",
     "parameters": {"type": "object", "properties": {
         "name": _string("Product name buyers see, e.g. 'Starter plan'"),
         "amount_usd": _number("Price, e.g. 19 for $19.00 (in the currency's main unit)"),
         "currency": _string("Three-letter currency code (default usd)"),
         "description": _string("Optional product description"),
         "recurring_interval": _string("month, year, week or day for a subscription; empty for a one-time price")},
         "required": ["name", "amount_usd"]}},
    {"name": "list_payment_links", "tag": "read", "fn": list_payment_links,
     "description": "List the newest 20 Stripe payment links: id, URL, whether active, when created.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_payment_link", "tag": "read", "fn": get_payment_link,
     "description": "Look up one Stripe payment link by id (plink_...) or URL: URL, whether it is active, the "
                    "product and price. Use it to check a payment link someone handed off.",
     "parameters": {"type": "object", "properties": {"id": _string("Payment link id (plink_...) or its URL")},
                    "required": ["id"]}},
    {"name": "recent_payments", "tag": "read", "fn": recent_payments,
     "description": "Successful Stripe payments in the last N days: when, amount, currency, customer email, "
                    "refunds.",
     "parameters": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "How many days back (default 7, at most 90)"}},
         "required": []}},
    {"name": "create_refund", "tag": "irreversible", "fn": create_refund, "takes_approval_id": True,
     "description": "Refund a Stripe payment (always waits for the owner's approval).",
     "parameters": {"type": "object", "properties": {
         "payment_intent": _string("The payment to refund: pi_... (or a charge id ch_...)"),
         "amount_usd": _number("How much to refund, in the payment's currency (e.g. 5 for $5.00)")},
         "required": ["payment_intent", "amount_usd"]}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


# ---------------------------------------------------------------- health and metrics

def health(inst: dict) -> tuple[str, str]:
    key = _key(inst)
    if not key:
        return "bad", "No key: paste a Stripe test key (sk_test_...) or a restricted key with the Agent tag"
    try:
        _request(inst, "GET", "/v1/balance", timeout=HEALTH_TIMEOUT)
    except StripeError as e:
        if e.status == 401:
            return "bad", "key rejected"
        if e.status == 403:
            return "bad", "the key may not read the balance (tick Balance: read on the restricted key)"
        return "bad", str(e)[:300]
    return "ok", f"{mode_of_key(key)} mode, balance available"


def metrics(inst: dict, company: dict | None) -> list[tuple[str, float, str]]:
    m = mode(inst)
    since = int(time.time()) - 30 * 86400
    charges, capped = _list_all(inst, "/v1/charges", {"created": {"gte": since}})
    paid = [c for c in charges if c.get("status") == "succeeded" and c.get("paid", True)]
    usd = [c for c in paid if (c.get("currency") or "").lower() == "usd"]
    other = [c for c in paid if (c.get("currency") or "").lower() != "usd"]
    revenue = sum(int(c.get("amount") or 0) - int(c.get("amount_refunded") or 0) for c in usd) / 100
    rev_note = f"{len(usd)} USD charge(s) in 30 days after refunds, {m} mode"
    if other:
        by_cur: dict[str, int] = {}
        for c in other:
            cur = (c.get("currency") or "?").lower()
            by_cur[cur] = by_cur.get(cur, 0) + int(c.get("amount") or 0) - int(c.get("amount_refunded") or 0)
        rev_note += "; not counted: " + ", ".join(f"{_money(v, k)}" for k, v in sorted(by_cur.items()))
    if capped:
        rev_note += f"; first {PAGE_CAP} charges only"

    customers, cust_capped = _list_all(inst, "/v1/customers")
    subs, _ = _list_all(inst, "/v1/subscriptions", {"status": "active"})
    mrr_minor = 0.0
    skipped = 0
    for s in subs:
        if s.get("status") not in (None, "active"):
            continue
        for item in ((s.get("items") or {}).get("data") or []):
            price = item.get("price") or item.get("plan") or {}
            recurring = price.get("recurring") or {}
            interval = recurring.get("interval") or price.get("interval") or ""
            unit = price.get("unit_amount", price.get("amount"))
            if unit is None or (price.get("currency") or "usd").lower() != "usd" or interval not in INTERVALS:
                skipped += 1
                continue
            mrr_minor += _monthly(int(unit), int(item.get("quantity") or 1), interval,
                                  int(recurring.get("interval_count") or price.get("interval_count") or 1))
    mrr_note = f"{len(subs)} active subscription(s), normalised to a month, {m} mode"
    if skipped:
        mrr_note += f"; {skipped} item(s) not in USD or without a unit price not counted"
    return [("revenue_usd_30d", round(revenue, 2), rev_note),
            ("payments_30d", float(len(paid)), f"successful charges in 30 days, {m} mode"),
            ("customers", float(len(customers)), f"{m} mode" + ("; count capped" if cust_capped else "")),
            ("mrr_usd", round(mrr_minor / 100, 2), mrr_note)]


DEFINITION = {
    "label": "Stripe",
    "category": "Payments",
    "kind": "api",
    "scope": "company",
    "setup": {
        "where_to_get_key": (
            "In the Stripe dashboard, switch to test mode, then Developers > API keys > Create restricted key. "
            "Give it the Agent tag: from December 2026 Stripe's hosted MCP server refuses keys without it, so "
            "create Agent-tagged keys from the start (migrate older keys by November 30, 2026). A test secret key "
            "(sk_test_...) also works for the typed tools."),
        "permissions": ["Balance: read", "Charges: read", "Customers: read", "Subscriptions: read",
                        "Products: write", "Prices: write", "Payment Links: write",
                        "Refunds: write (only if agents may propose refunds; each one asks you)"],
        "fields": [
            {"key": "api_key", "label": "Secret or restricted key", "secret": True,
             "hint": "A test key (sk_test_... or rk_test_...) while you try it; a live key per company when ready",
             "placeholder": "rk_test_..."},
            {"key": "use_mcp", "label": "Also use Stripe's hosted MCP server", "secret": False, "type": "bool",
             "hint": "yes attaches https://mcp.stripe.com (the Stripe MCP card) for what the typed tools do not "
                     "cover; it needs an Agent-tagged key",
             "placeholder": "no"},
            {"key": "api_base", "label": "API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": API_BASE},
        ],
        "test_mode": ("Start with a test key; flip to a live key per company when you are ready. The mode comes "
                      "from the key (sk_test_ and rk_test_ keys are test mode) and is shown here and to agents."),
        "docs_url": "https://docs.stripe.com/keys",
        "agents_can": ["Create payment links (product, price and link) for an offer",
                       "List payment links and inspect one to check a handoff",
                       "Read recent payments",
                       "Propose refunds: refunds, and anything that moves money out, always ask you"],
    },
    "health": health,
    "metrics": metrics,
    "tools": tools,
    "test_mode": test_mode,
}


# ---------------------------------------------------------------- the hosted MCP server (optional)

# The hosted server's tool names (verified Oct 14, 2026). Ordered: the first matching glob wins. A tool no named
# entry matches is irreversible (the last entry): a payments server's unknown tool may move money, so it is locked
# to the owner's approval. stripe_api_write is irreversible too unless tag_for_call finds a safe target.
MCP_CLASSIFY = [
    ("stripe_api_search", "read"), ("stripe_api_details", "read"), ("stripe_api_read", "read"),
    ("get_stripe_account_info", "read"), ("stripe_analytics", "read"), ("search_stripe_documentation", "read"),
    ("stripe_implementation_planner", "read"),
    ("stripe_api_write", "irreversible"),
    ("send_money", "spend"), ("transfer_money", "spend"), ("convert_currency", "spend"),
    ("setup_recipient", "write"),
    ("atlas_company_formation", "irreversible"), ("send_stripe_feedback", "irreversible"),
    ("*", "irreversible"),
]
MCP_READ = [g for g, t in MCP_CLASSIFY if t == "read"]
# Read tools for every role; the rest only for the roles that handle money and the product's checkout.
MCP_ALLOW = {"*": list(MCP_READ), "ceo": ["*"], "engineering": ["*"], "ops": ["*"], "analyst": ["*"],
             "sales": ["*"]}
_DESTRUCTIVE = ("refund", "cancel", "delete", "dispute", "reverse", "void")
_MONEY_OUT = ("payout", "transfer")
# The only writes that ask under connector_publish: creating or updating products, prices and payment links
# (POST /v1/products, POST /v1/prices/price_1, ...). Anything else stripe_api_write can do is irreversible.
SAFE_WRITE_TARGETS = ("products", "prices", "payment_links")
_SAFE_PATH = re.compile(r"^/?(?:v1/)?(?:" + "|".join(SAFE_WRITE_TARGETS) + r")(?:/[A-Za-z0-9_]+)?/?$")
_TARGET_KEYS = ("path", "endpoint", "url", "resource")


def classify(tool: str) -> str | None:
    for glob, tag in MCP_CLASSIFY:
        if fnmatch.fnmatchcase(tool, glob):
            return tag
    return None


def tag_for_call(tool: str, args: dict | None) -> str | None:
    """The tag of one stripe_api_write call (the definition lets this answer lower the map's irreversible):
    publish only for a POST that creates or updates a product, a price or a payment link, named by one path;
    spend for a payout or a transfer; irreversible for everything else (refunds, cancellations, deletions, and
    any target Holdco cannot read). None for other tools keeps the map's tag."""
    if tool != "stripe_api_write":
        return None
    args = args if isinstance(args, dict) else {}
    blob = json.dumps(args, sort_keys=True, default=str).lower()
    if any(w in blob for w in _DESTRUCTIVE):
        return "irreversible"
    if any(w in blob for w in _MONEY_OUT):
        return "spend"
    method = str(args.get("method") or args.get("http_method") or "POST").strip().upper()
    targets = [str(args[k]).strip() for k in _TARGET_KEYS if args.get(k)]
    if method != "POST" or len(targets) != 1:
        return "irreversible"
    path = urllib.parse.urlparse(targets[0]).path if "://" in targets[0] else targets[0].split("?", 1)[0]
    return "publish" if _SAFE_PATH.match(path) else "irreversible"


def _company_of(inst: dict) -> dict | None:
    if not inst.get("company_id"):
        return None
    from . import db
    return db.get_company(int(inst["company_id"]))


def _parent(inst: dict) -> dict | None:
    """The Stripe (api) instance this MCP instance belongs to: the company's, else the global one."""
    from . import connectors
    try:
        return connectors.instance("stripe", _company_of(inst))
    except Exception:
        return None


def mcp_key(inst: dict) -> str:
    """The MCP instance's own key, or else the Stripe card's key."""
    own = _key(inst)
    return own or _key(_parent(inst))


def mcp_url(inst: dict) -> str:
    return (str(_cfg(inst).get("mcp_url") or "").strip() or MCP_URL).rstrip("/")


def mcp_headers(inst: dict) -> dict:
    key = mcp_key(inst)
    return {"Authorization": f"Bearer {key}"} if key else {}


def mcp_active(inst: dict) -> bool:
    """The MCP part works only while the Stripe card has use_mcp ticked."""
    parent = _parent(inst)
    return bool(parent and parent.get("enabled", 1) and use_mcp(parent))


def mcp_health(inst: dict) -> tuple[str, str]:
    if not mcp_active(inst):
        return "warn", "Off: tick use_mcp on the Stripe card to attach Stripe's hosted MCP server"
    if not mcp_key(inst):
        return "bad", "No key: add one on the Stripe card"
    from . import connectors, mcp
    try:
        listed = connectors.client(inst).list_tools()
    except mcp.AuthRequired:
        return "bad", ("Stripe asked for sign-in: use a restricted key with the Agent tag, or press Sign in "
                       "on this card")
    except Exception as e:
        return "bad", _scrub(str(e), mcp_key(inst))[:300]
    return "ok", f"{len(listed)} Stripe MCP tools, {mode_of_key(mcp_key(inst))} mode"


MCP_DEFINITION = {
    "label": "Stripe MCP (hosted)",
    "category": "Payments",
    "kind": "mcp-http",
    "scope": "company",
    "parent": "stripe",               # works only while the Stripe card has use_mcp ticked
    "parent_field": "use_mcp",
    "setup": {
        "where_to_get_key": ("Nothing new: this uses the key on the Stripe card. It must be a restricted key with "
                             "the Agent tag (required from December 2026; migrate by November 30, 2026). Tick "
                             "use_mcp on the Stripe card to turn this on."),
        "permissions": ["The same permissions as the Stripe card"],
        "fields": [
            {"key": "api_key", "label": "Agent-tagged key (optional)", "secret": True,
             "hint": "Leave blank to use the Stripe card's key", "placeholder": "rk_test_..."},
            {"key": "mcp_url", "label": "Server address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": MCP_URL},
        ],
        "test_mode": "Follows the key: a test key works on test data only.",
        "docs_url": "https://docs.stripe.com/mcp",
        "agents_can": ["Search and read Stripe objects and documentation",
                       "Propose other Stripe changes: writes ask you; refunds, cancellations, deletions and "
                       "money movements are locked to your approval"],
    },
    "health": mcp_health,
    "test_mode": lambda inst: mode_of_key(mcp_key(inst)) == "test",
    "mcp": {"url": MCP_URL, "headers": {}, "classify": MCP_CLASSIFY, "allow": MCP_ALLOW, "blocked": []},
    # Callables the framework uses when present: the server address (tests override it) and the auth header.
    "mcp_url": mcp_url,
    "mcp_headers": mcp_headers,
    "active": mcp_active,
    "tag_for_call": tag_for_call,
    "tag_for_call_lowers": True,      # tag_for_call decides stripe_api_write's tag outright (see there)
}

# Both definitions this module provides, by key (connectors.DEFINITIONS merges them).
DEFINITIONS = {"stripe": DEFINITION, "stripe_mcp": MCP_DEFINITION}
