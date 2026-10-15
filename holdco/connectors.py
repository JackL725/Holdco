"""Connectors to the outside services a small online business runs on, standard library only.

Every connector is a few per-company settings (see config.CONNECTORS) plus the calls agents are allowed to make
through tools. Agents never see tokens: tools call these functions, which read the company's settings.

Payments   Stripe, Lemon Squeezy, Gumroad      read balance, sales, refunds, fees, subscriptions; sync into the ledger
Analytics  Plausible, GoatCounter              visitors, pageviews, top pages and sources
Website    Netlify                              deploys (actions.py) and form submissions (signups)
Social     X, Bluesky, Mastodon, LinkedIn, webhook   publish approved posts directly
Scheduling Cal.com                              upcoming bookings
Domains    RDAP (no account needed)             is a domain name taken?
Owner      Slack/Discord webhook, Telegram, ntfy  notify you when agents need a decision or report something

Base URLs can be overridden with HOLDCO_API_BASE_<NAME> environment variables (the tests point them at a fake server).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

UA = "Holdco/1.0 (+https://github.com/JackL725/Holdco)"
LINKEDIN_VERSION = "202506"
CALCOM_VERSION = "2024-08-13"

BASES = {
    "stripe": "https://api.stripe.com", "lemonsqueezy": "https://api.lemonsqueezy.com", "gumroad": "https://api.gumroad.com",
    "netlify": "https://api.netlify.com", "x": "https://api.x.com", "linkedin": "https://api.linkedin.com",
    "rdap": "https://rdap.org", "telegram": "https://api.telegram.org", "ntfy": "https://ntfy.sh",
    "plausible": "https://plausible.io", "bluesky": "https://bsky.social", "calcom": "https://api.cal.com",
    "goatcounter": "", "mastodon": "",
}


class ConnectorError(Exception):
    pass


def base(name: str, configured_url: str = "") -> str:
    env = os.environ.get(f"HOLDCO_API_BASE_{name.upper()}")
    url = (env or configured_url or BASES.get(name, "")).strip().rstrip("/")
    if not url:
        raise ConnectorError(f"No server address set for {name}.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ---------------------------------------------------------------- configuration

def configured(cs: dict, key: str) -> bool:
    c = config.CONNECTORS.get(key)
    return bool(c) and all(str(cs.get(k) or "").strip() for k in c["required"])


def capabilities(cs: dict) -> set[str]:
    caps: set[str] = set()
    for key, c in config.CONNECTORS.items():
        if not configured(cs, key):
            continue
        for cap in c["capabilities"]:
            if cap == "imap" and not all(cs.get(k) for k in ("imap_host", "imap_user", "imap_password")):
                continue
            if cap == "forms" and not cs.get("netlify_site_id"):
                continue
            caps.add(cap)
    return caps


def has(cs: dict, requirement: str) -> bool:
    """Does the company satisfy a routine's or tool's requirement ('', 'smtp', 'payments', 'social:x'...)?"""
    return not requirement or requirement in capabilities(cs)


def provider_for(cs: dict, capability: str) -> str | None:
    for key, c in config.CONNECTORS.items():
        if capability in c["capabilities"] and configured(cs, key) and capability in capabilities(cs):
            return key
    return None


def status(cs: dict) -> dict[str, dict]:
    caps = capabilities(cs)
    out = {}
    for key, c in config.CONNECTORS.items():
        on = configured(cs, key)
        out[key] = {"configured": on, "capabilities": [cap for cap in c["capabilities"] if cap in caps]}
    return out


def catalogue() -> dict:
    """What the dashboard needs to render the Connectors tab."""
    items = []
    for key, c in config.CONNECTORS.items():
        items.append({"key": key, **{k: c[k] for k in ("label", "category", "blurb", "docs", "fields", "required", "capabilities")}})
    return {"connectors": items, "categories": config.CONNECTOR_CATEGORIES}


# ---------------------------------------------------------------- HTTP

def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def _http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
          timeout: float = 30) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(100_000).decode("utf-8", "replace"), dict(e.headers or {})
    except urllib.error.URLError as e:
        raise ConnectorError(f"Couldn't reach {_host(url)}: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise ConnectorError(f"Timed out talking to {_host(url)}.") from None
    except (ConnectionError, OSError, ValueError) as e:
        raise ConnectorError(f"Couldn't talk to {_host(url)}: {e}") from None


def _error_text(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:200]
    if isinstance(data, dict):
        err = data.get("error") or data.get("errors") or data.get("message") or data.get("detail") or data.get("title")
        if isinstance(err, dict):
            err = err.get("message") or err.get("detail") or json.dumps(err)
        if isinstance(err, list) and err:
            first = err[0]
            err = first.get("detail") or first.get("message") or first.get("title") or json.dumps(first) if isinstance(first, dict) else first
        if err:
            return str(err)[:200]
    return " ".join(text.split())[:200]


def call(method: str, url: str, headers: dict | None = None, json_body=None, form: dict | None = None,
         raw: bytes | None = None, ok: tuple = (200, 201, 202, 204), timeout: float = 30,
         want_headers: bool = False):
    """One API call. Raises ConnectorError with a readable message on any non-2xx reply."""
    h = dict(headers or {})
    body = raw
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form, doseq=True).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if body is not None and method == "GET":
        method = "POST"
    status_code, text, resp_headers = _http(method, url, h, body, timeout)
    if status_code not in ok:
        hint = ""
        if status_code in (401, 403):
            hint = " (check the token or key)"
        elif status_code == 429:
            hint = " (rate limited; try later)"
        raise ConnectorError(f"HTTP {status_code} from {_host(url)}{hint}: {_error_text(text)}")
    data = {}
    if text.strip():
        try:
            data = json.loads(text)
        except ValueError:
            data = {"_text": text[:2000]}
    return (data, resp_headers) if want_headers else data


def _qs(params: dict) -> str:
    return "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, doseq=True)


def _day(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(ts)[:10]


def _since(days: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=max(1, int(days or 30)))).isoformat()


def _money(amount: float, currency: str) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), "")
    sign = "-" if amount < 0 else ""
    return f"{sign}{sym}{abs(amount):,.2f}" + ("" if sym else f" {currency.upper()}")


# ---------------------------------------------------------------- payments: Stripe

def _stripe(cs: dict, path: str, params: dict | None = None):
    return call("GET", base("stripe") + path + (_qs(params) if params else ""),
                {"Authorization": f"Bearer {cs['stripe_secret_key'].strip()}"})


def _stripe_list(cs: dict, path: str, params: dict, pages: int = 3) -> list[dict]:
    out: list[dict] = []
    params = dict(params)
    for _ in range(pages):
        data = _stripe(cs, path, params)
        out.extend(data.get("data") or [])
        if not data.get("has_more") or not out:
            break
        params["starting_after"] = out[-1]["id"]
    return out


def _monthly(price: dict, quantity: int) -> float:
    rec = price.get("recurring") or {}
    unit = (price.get("unit_amount") or 0) / 100
    n = int(rec.get("interval_count") or 1)
    months = {"day": n / 30.0, "week": n / 4.345, "month": float(n), "year": 12.0 * n}.get(rec.get("interval"), 1.0)
    return unit * max(1, int(quantity or 1)) / (months or 1.0)


def stripe_transactions(cs: dict, days: int = 30) -> list[dict]:
    since = int(time.time() - max(1, int(days or 30)) * 86400)
    rows = []
    for t in _stripe_list(cs, "/v1/balance_transactions", {"limit": 100, "created[gte]": since}):
        typ, cur, day = t.get("type"), (t.get("currency") or "usd").upper(), _day(t.get("created"))
        amt = (t.get("amount") or 0) / 100
        desc = t.get("description") or ""
        if typ in ("charge", "payment"):
            rows.append({"external_id": t["id"], "date": day, "amount": amt, "currency": cur, "direction": "in", "category": "revenue",
                         "counterparty": "", "description": desc or "Stripe payment", "provider": "stripe"})
            if t.get("fee"):
                rows.append({"external_id": f"{t['id']}:fee", "date": day, "amount": t["fee"] / 100, "currency": cur, "direction": "out",
                             "category": "fees", "counterparty": "Stripe", "description": f"Processing fee for {t['id']}", "provider": "stripe"})
        elif typ in ("refund", "payment_refund", "payment_failure_refund"):
            rows.append({"external_id": t["id"], "date": day, "amount": abs(amt), "currency": cur, "direction": "out", "category": "refunds",
                         "counterparty": "", "description": desc or "Refund", "provider": "stripe"})
            if t.get("fee"):  # Stripe returns part of the fee on refund (negative fee)
                rows.append({"external_id": f"{t['id']}:fee", "date": day, "amount": abs(t["fee"]) / 100, "currency": cur,
                             "direction": "in" if t["fee"] < 0 else "out", "category": "fees", "counterparty": "Stripe",
                             "description": f"Fee adjustment for {t['id']}", "provider": "stripe"})
        elif typ in ("stripe_fee", "application_fee", "tax_fee"):
            rows.append({"external_id": t["id"], "date": day, "amount": abs(amt), "currency": cur, "direction": "out", "category": "fees",
                         "counterparty": "Stripe", "description": desc or typ.replace("_", " "), "provider": "stripe"})
        elif typ in ("adjustment", "dispute", "payment_reversal"):
            rows.append({"external_id": t["id"], "date": day, "amount": abs(amt), "currency": cur, "direction": "in" if amt > 0 else "out",
                         "category": "refunds" if amt < 0 else "other", "counterparty": "", "description": desc or typ, "provider": "stripe"})
        # payouts and transfers move money between Stripe and the bank; they aren't income or expenses.
    return rows


def stripe_summary(cs: dict, days: int = 30) -> dict:
    bal = _stripe(cs, "/v1/balance")
    avail = bal.get("available") or []
    currency = (avail[0].get("currency") if avail else "usd").upper()
    available = sum((a.get("amount") or 0) for a in avail) / 100
    pending = sum((a.get("amount") or 0) for a in (bal.get("pending") or [])) / 100
    txs = stripe_transactions(cs, days)
    gross = sum(r["amount"] for r in txs if r["category"] == "revenue")
    refunds = sum(r["amount"] for r in txs if r["category"] == "refunds" and r["direction"] == "out")
    fees = sum(r["amount"] if r["direction"] == "out" else -r["amount"] for r in txs if r["category"] == "fees")
    subs = _stripe_list(cs, "/v1/subscriptions", {"status": "active", "limit": 100})
    mrr = 0.0
    for s in subs:
        for item in ((s.get("items") or {}).get("data") or []):
            mrr += _monthly(item.get("price") or {}, item.get("quantity") or 1)
    recent = [r for r in txs if r["category"] in ("revenue", "refunds")][:10]
    return {"provider": "stripe", "currency": currency, "days": days, "balance_available": available, "balance_pending": pending,
            "sales_count": sum(1 for r in txs if r["category"] == "revenue"), "gross": gross, "refunds": refunds, "fees": fees,
            "net": gross - refunds - fees, "subscriptions_active": len(subs), "mrr": mrr, "recent": recent}


# ---------------------------------------------------------------- payments: Lemon Squeezy

def _ls(cs: dict, path: str, params: dict | None = None):
    return call("GET", base("lemonsqueezy") + path + (_qs(params) if params else ""),
                {"Authorization": f"Bearer {cs['lemonsqueezy_api_key'].strip()}", "Accept": "application/vnd.api+json"})


def _ls_list(cs: dict, path: str, params: dict, pages: int = 3) -> list[dict]:
    out: list[dict] = []
    data = _ls(cs, path, params)
    for _ in range(pages):
        out.extend(data.get("data") or [])
        nxt = (data.get("links") or {}).get("next")
        if not nxt:
            break
        data = call("GET", nxt, {"Authorization": f"Bearer {cs['lemonsqueezy_api_key'].strip()}", "Accept": "application/vnd.api+json"})
    return out


def lemonsqueezy_transactions(cs: dict, days: int = 30) -> list[dict]:
    since = _since(days)
    params = {"page[size]": 100, "sort": "-createdAt"}
    if cs.get("lemonsqueezy_store_id"):
        params["filter[store_id]"] = cs["lemonsqueezy_store_id"]
    rows = []
    for o in _ls_list(cs, "/v1/orders", params):
        a = o.get("attributes") or {}
        day = str(a.get("created_at") or "")[:10]
        if day < since or a.get("status") in ("pending", "failed"):
            continue
        cur = (a.get("currency") or "USD").upper()
        who = a.get("user_email") or a.get("user_name") or ""
        item = ((a.get("first_order_item") or {}).get("product_name") or "")
        rows.append({"external_id": str(o.get("id")), "date": day, "amount": (a.get("total") or 0) / 100, "currency": cur,
                     "direction": "in", "category": "revenue", "counterparty": who,
                     "description": f"Lemon Squeezy order {a.get('identifier') or o.get('id')}" + (f": {item}" if item else ""), "provider": "lemonsqueezy"})
        if a.get("refunded") or a.get("status") in ("refunded", "partial_refund"):
            amt = (a.get("refunded_amount") or a.get("total") or 0) / 100
            rows.append({"external_id": f"{o.get('id')}:refund", "date": str(a.get("refunded_at") or a.get("updated_at") or day)[:10],
                         "amount": amt, "currency": cur, "direction": "out", "category": "refunds", "counterparty": who,
                         "description": f"Refund of order {a.get('identifier') or o.get('id')}", "provider": "lemonsqueezy"})
    return rows


def lemonsqueezy_summary(cs: dict, days: int = 30) -> dict:
    txs = lemonsqueezy_transactions(cs, days)
    gross = sum(r["amount"] for r in txs if r["category"] == "revenue")
    refunds = sum(r["amount"] for r in txs if r["category"] == "refunds")
    currency = txs[0]["currency"] if txs else "USD"
    params = {"page[size]": 100, "filter[status]": "active"}
    if cs.get("lemonsqueezy_store_id"):
        params["filter[store_id]"] = cs["lemonsqueezy_store_id"]
    subs = _ls_list(cs, "/v1/subscriptions", params)
    mrr, priced = 0.0, 0
    for s in subs[:30]:
        item = ((s.get("attributes") or {}).get("first_subscription_item") or {})
        if not item.get("price_id"):
            continue
        try:
            p = (_ls(cs, f"/v1/prices/{item['price_id']}").get("data") or {}).get("attributes") or {}
        except ConnectorError:
            continue
        unit = (p.get("unit_price") or 0) / 100
        n = int(p.get("renewal_interval_quantity") or 1)
        months = {"day": n / 30.0, "week": n / 4.345, "month": float(n), "year": 12.0 * n}.get(p.get("renewal_interval_unit"), 1.0)
        mrr += unit * int(item.get("quantity") or 1) / (months or 1.0)
        priced += 1
    return {"provider": "lemonsqueezy", "currency": currency, "days": days, "balance_available": None, "balance_pending": None,
            "sales_count": sum(1 for r in txs if r["category"] == "revenue"), "gross": gross, "refunds": refunds, "fees": 0.0,
            "net": gross - refunds, "subscriptions_active": len(subs), "mrr": mrr if priced == len(subs[:30]) and priced else None,
            "recent": txs[:10]}


# ---------------------------------------------------------------- payments: Gumroad

def _gumroad(cs: dict, path: str, params: dict | None = None):
    p = {"access_token": cs["gumroad_access_token"].strip(), **(params or {})}
    data = call("GET", base("gumroad") + path + _qs(p))
    if data.get("success") is False:
        raise ConnectorError(f"Gumroad: {data.get('message') or 'request failed'}")
    return data


def gumroad_transactions(cs: dict, days: int = 30) -> list[dict]:
    rows = []
    params = {"after": _since(days)}
    sym = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
    for _ in range(3):
        data = _gumroad(cs, "/v2/sales", params)
        for s in data.get("sales") or []:
            cur = sym.get(s.get("currency_symbol") or "$", "USD")
            day = str(s.get("created_at") or "")[:10]
            price = (s.get("price") or 0) / 100
            rows.append({"external_id": str(s.get("id")), "date": day, "amount": price, "currency": cur, "direction": "in", "category": "revenue",
                         "counterparty": s.get("email") or s.get("purchase_email") or "", "description": f"Gumroad sale: {s.get('product_name') or ''}".strip(),
                         "provider": "gumroad", "_recurrence": s.get("recurrence"), "_sub": s.get("subscription_id")})
            if s.get("gumroad_fee"):
                rows.append({"external_id": f"{s.get('id')}:fee", "date": day, "amount": (s["gumroad_fee"] or 0) / 100, "currency": cur,
                             "direction": "out", "category": "fees", "counterparty": "Gumroad", "description": f"Gumroad fee for sale {s.get('id')}",
                             "provider": "gumroad"})
            if s.get("refunded"):
                rows.append({"external_id": f"{s.get('id')}:refund", "date": day, "amount": price, "currency": cur, "direction": "out",
                             "category": "refunds", "counterparty": s.get("email") or "", "description": f"Refund of sale {s.get('id')}",
                             "provider": "gumroad"})
        if not data.get("next_page_key"):
            break
        params["page_key"] = data["next_page_key"]
    return rows


def gumroad_summary(cs: dict, days: int = 30) -> dict:
    txs = gumroad_transactions(cs, days)
    gross = sum(r["amount"] for r in txs if r["category"] == "revenue")
    refunds = sum(r["amount"] for r in txs if r["category"] == "refunds")
    fees = sum(r["amount"] for r in txs if r["category"] == "fees")
    subs = {r["_sub"] for r in txs if r.get("_sub") and r["category"] == "revenue"}
    per = {"monthly": 1.0, "quarterly": 3.0, "biannually": 6.0, "yearly": 12.0, "every_two_years": 24.0}
    mrr = sum(r["amount"] / per.get(r.get("_recurrence") or "", 1.0) for r in txs if r.get("_sub") and r["category"] == "revenue")
    return {"provider": "gumroad", "currency": txs[0]["currency"] if txs else "USD", "days": days, "balance_available": None,
            "balance_pending": None, "sales_count": sum(1 for r in txs if r["category"] == "revenue"), "gross": gross, "refunds": refunds,
            "fees": fees, "net": gross - refunds - fees, "subscriptions_active": len(subs), "mrr": mrr if subs else None, "recent": txs[:10]}


# ---------------------------------------------------------------- payments: unified

PAYMENTS = {"stripe": (stripe_summary, stripe_transactions), "lemonsqueezy": (lemonsqueezy_summary, lemonsqueezy_transactions),
            "gumroad": (gumroad_summary, gumroad_transactions)}


def payments_provider(cs: dict) -> str | None:
    return provider_for(cs, "payments")


def payments_summary(cs: dict, days: int = 30) -> dict:
    p = payments_provider(cs)
    if not p:
        raise ConnectorError("No payment provider is connected. The owner can add Stripe, Lemon Squeezy or Gumroad on the Connectors tab.")
    return PAYMENTS[p][0](cs, days)


def payments_transactions(cs: dict, days: int = 30) -> list[dict]:
    p = payments_provider(cs)
    if not p:
        raise ConnectorError("No payment provider is connected. The owner can add Stripe, Lemon Squeezy or Gumroad on the Connectors tab.")
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in PAYMENTS[p][1](cs, days)]


def payments_text(s: dict) -> str:
    cur = s["currency"]
    lines = [f"{config.CONNECTORS[s['provider']]['label']}, last {s['days']} days:"]
    if s.get("balance_available") is not None:
        lines.append(f"- Balance: {_money(s['balance_available'], cur)} available, {_money(s.get('balance_pending') or 0, cur)} pending")
    lines.append(f"- Sales: {s['sales_count']} for {_money(s['gross'], cur)} gross; refunds {_money(s['refunds'], cur)}; fees "
                 f"{_money(s['fees'], cur)}; net {_money(s['net'], cur)}")
    sub = f"- Active subscriptions: {s['subscriptions_active']}"
    if s.get("mrr") is not None:
        sub += f"; MRR about {_money(s['mrr'], cur)}"
    lines.append(sub)
    if s["recent"]:
        lines.append("Most recent:")
        lines += [f"- {r['date']} {'+' if r['direction'] == 'in' else '-'}{_money(r['amount'], r['currency'])} {r['description']}" for r in s["recent"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- analytics

PERIODS = {"7d": 7, "30d": 30, "12mo": 365, "month": 30, "day": 1, "6mo": 182}


def plausible_stats(cs: dict, period: str = "7d") -> dict:
    url = base("plausible", cs.get("plausible_url")) + "/api/v2/query"
    h = {"Authorization": f"Bearer {cs['plausible_api_key'].strip()}"}
    site = cs["plausible_site_id"].strip()
    period = period if period in PERIODS else "7d"
    main = call("POST", url, h, {"site_id": site, "metrics": ["visitors", "visits", "pageviews", "bounce_rate", "visit_duration"],
                                 "date_range": period})
    m = ((main.get("results") or [{}])[0].get("metrics") or [0, 0, 0, 0, 0]) + [0] * 5
    out = {"provider": "plausible", "period": period, "visitors": int(m[0] or 0), "visits": int(m[1] or 0), "pageviews": int(m[2] or 0),
           "bounce_rate": m[3], "visit_duration": m[4], "top_pages": [], "top_sources": []}
    for dim, key in (("event:page", "top_pages"), ("visit:source", "top_sources")):
        try:
            r = call("POST", url, h, {"site_id": site, "metrics": ["visitors"], "date_range": period, "dimensions": [dim],
                                      "order_by": [["visitors", "desc"]], "pagination": {"limit": 8}})
            out[key] = [{"name": (row.get("dimensions") or ["?"])[0], "visitors": int((row.get("metrics") or [0])[0] or 0)}
                        for row in r.get("results") or []]
        except ConnectorError:
            pass
    return out


def goatcounter_stats(cs: dict, period: str = "7d") -> dict:
    url = base("goatcounter", cs.get("goatcounter_url"))
    h = {"Authorization": f"Bearer {cs['goatcounter_api_key'].strip()}", "Content-Type": "application/json"}
    period = period if period in PERIODS else "7d"
    end = _dt.date.today()
    start = end - _dt.timedelta(days=PERIODS[period] - 1)
    q = _qs({"start": start.isoformat(), "end": end.isoformat()})
    total = call("GET", f"{url}/api/v0/stats/total{q}", h)
    out = {"provider": "goatcounter", "period": period, "visitors": int(total.get("total") or 0), "visits": int(total.get("total") or 0),
           "pageviews": int(total.get("total_events") or total.get("total") or 0), "bounce_rate": None, "visit_duration": None,
           "top_pages": [], "top_sources": []}
    try:
        hits = call("GET", f"{url}/api/v0/stats/hits{q}&limit=8", h)
        out["top_pages"] = [{"name": p.get("path") or "?", "visitors": int(p.get("count") or 0)} for p in hits.get("hits") or []]
    except ConnectorError:
        pass
    try:
        refs = call("GET", f"{url}/api/v0/stats/toprefs{q}&limit=8", h)
        out["top_sources"] = [{"name": r.get("name") or r.get("id") or "?", "visitors": int(r.get("count") or 0)} for r in refs.get("stats") or []]
    except ConnectorError:
        pass
    return out


ANALYTICS = {"plausible": plausible_stats, "goatcounter": goatcounter_stats}


def site_stats(cs: dict, period: str = "7d") -> dict:
    p = provider_for(cs, "analytics")
    if not p:
        raise ConnectorError("No analytics is connected. The owner can add Plausible or GoatCounter on the Connectors tab.")
    return ANALYTICS[p](cs, period)


def stats_text(s: dict) -> str:
    lines = [f"{config.CONNECTORS[s['provider']]['label']}, last {s['period']}: {s['visitors']} visitors, {s['visits']} visits, "
             f"{s['pageviews']} pageviews"]
    if s.get("bounce_rate") is not None:
        lines[0] += f", bounce rate {s['bounce_rate']}%"
    if s["top_pages"]:
        lines.append("Top pages: " + ", ".join(f"{p['name']} ({p['visitors']})" for p in s["top_pages"]))
    if s["top_sources"]:
        lines.append("Top sources: " + ", ".join(f"{p['name']} ({p['visitors']})" for p in s["top_sources"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- website: Netlify

def _netlify_headers(cs: dict) -> dict:
    return {"Authorization": f"Bearer {cs['netlify_token'].strip()}"}


def netlify_submissions(cs: dict, limit: int = 50) -> list[dict]:
    if not cs.get("netlify_site_id"):
        raise ConnectorError("The Netlify site hasn't been created yet (deploy the site once), so there are no forms to read.")
    rows = call("GET", f"{base('netlify')}/api/v1/sites/{cs['netlify_site_id']}/submissions{_qs({'per_page': max(1, min(int(limit or 50), 100))})}",
                _netlify_headers(cs))
    out = []
    for s in rows if isinstance(rows, list) else []:
        data = s.get("data") or {}
        email = s.get("email") or next((v for k, v in data.items() if "email" in k.lower() and isinstance(v, str) and "@" in v), "")
        name = s.get("name") or data.get("name") or data.get("full_name") or ""
        extra = {k: v for k, v in data.items() if k not in ("email", "name", "ip", "user_agent", "referrer", "bot-field") and isinstance(v, str) and v}
        out.append({"id": s.get("id"), "form": s.get("form_name") or "", "email": email, "name": name, "created_at": s.get("created_at") or "",
                    "fields": extra})
    return out


# ---------------------------------------------------------------- social

def _oauth1_header(method: str, url: str, params: dict, key: str, secret: str, token: str, token_secret: str,
                   nonce: str = "", timestamp: str = "") -> str:
    """OAuth 1.0a (HMAC-SHA1) Authorization header, as X's API requires for user-context posting."""
    q = lambda s: urllib.parse.quote(str(s), safe="")  # noqa: E731
    oauth = {"oauth_consumer_key": key, "oauth_nonce": nonce or secrets.token_hex(16), "oauth_signature_method": "HMAC-SHA1",
             "oauth_timestamp": timestamp or str(int(time.time())), "oauth_token": token, "oauth_version": "1.0"}
    all_params = {**params, **oauth}
    # percent-encode keys and values first, sort the encoded pairs, then join (RFC 5849 section 3.4.1.3.2)
    param_str = "&".join(f"{k}={v}" for k, v in sorted((q(k), q(v)) for k, v in all_params.items()))
    base_str = "&".join([method.upper(), q(url), q(param_str)])
    signing_key = f"{q(secret)}&{q(token_secret)}"
    sig = base64.b64encode(hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{q(k)}="{q(v)}"' for k, v in sorted(oauth.items()))


def x_post(cs: dict, text: str, link: str = "") -> str:
    text = _with_link(text, link, 280)
    url = base("x") + "/2/tweets"
    auth = _oauth1_header("POST", url, {}, cs["x_api_key"].strip(), cs["x_api_secret"].strip(), cs["x_access_token"].strip(),
                          cs["x_access_secret"].strip())
    data = call("POST", url, {"Authorization": auth}, {"text": text})
    tid = (data.get("data") or {}).get("id", "")
    return f"Posted on X: https://x.com/i/web/status/{tid}" if tid else "Posted on X."


def _x_me(cs: dict) -> dict:
    url = base("x") + "/2/users/me"
    auth = _oauth1_header("GET", url, {}, cs["x_api_key"].strip(), cs["x_api_secret"].strip(), cs["x_access_token"].strip(),
                          cs["x_access_secret"].strip())
    return call("GET", url, {"Authorization": auth}).get("data") or {}


def _bluesky_session(cs: dict) -> dict:
    return call("POST", base("bluesky", cs.get("bluesky_url")) + "/xrpc/com.atproto.server.createSession", None,
                {"identifier": cs["bluesky_handle"].strip().lstrip("@"), "password": cs["bluesky_app_password"].strip()})


def _bluesky_facets(text: str) -> list[dict]:
    facets = []
    for m in re.finditer(r"https?://[^\s<>\"')\]]+", text):
        start = len(text[:m.start()].encode("utf-8"))
        end = start + len(m.group(0).encode("utf-8"))
        facets.append({"index": {"byteStart": start, "byteEnd": end},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}]})
    return facets


def bluesky_post(cs: dict, text: str, link: str = "") -> str:
    text = _with_link(text, link, 300)
    sess = _bluesky_session(cs)
    record = {"$type": "app.bsky.feed.post", "text": text,
              "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    facets = _bluesky_facets(text)
    if facets:
        record["facets"] = facets
    data = call("POST", base("bluesky", cs.get("bluesky_url")) + "/xrpc/com.atproto.repo.createRecord",
                {"Authorization": f"Bearer {sess.get('accessJwt', '')}"},
                {"repo": sess.get("did"), "collection": "app.bsky.feed.post", "record": record})
    rkey = str(data.get("uri") or "").rsplit("/", 1)[-1]
    handle = sess.get("handle") or cs["bluesky_handle"].strip().lstrip("@")
    return f"Posted on Bluesky: https://bsky.app/profile/{handle}/post/{rkey}" if rkey else "Posted on Bluesky."


def mastodon_post(cs: dict, text: str, link: str = "") -> str:
    text = _with_link(text, link, 500)
    data = call("POST", base("mastodon", cs["mastodon_url"]) + "/api/v1/statuses",
                {"Authorization": f"Bearer {cs['mastodon_access_token'].strip()}", "Idempotency-Key": hashlib.sha1(text.encode()).hexdigest()},
                form={"status": text})
    return f"Posted on Mastodon: {data.get('url')}" if data.get("url") else "Posted on Mastodon."


def _linkedin_headers(cs: dict) -> dict:
    return {"Authorization": f"Bearer {cs['linkedin_access_token'].strip()}", "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": LINKEDIN_VERSION}


def linkedin_author(cs: dict) -> str:
    urn = (cs.get("linkedin_author_urn") or "").strip()
    if urn:
        return urn
    me = call("GET", base("linkedin") + "/v2/userinfo", _linkedin_headers(cs))
    if not me.get("sub"):
        raise ConnectorError("LinkedIn didn't return a member id; the token needs the openid and profile scopes.")
    return f"urn:li:person:{me['sub']}"


def linkedin_post(cs: dict, text: str, link: str = "") -> str:
    text = _with_link(text, link, 3000)
    escaped = re.sub(r"([\\()\[\]{}<>@|~_*#])", r"\\\1", text)   # LinkedIn's "little text" format escapes these
    body = {"author": linkedin_author(cs), "commentary": escaped, "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    _, headers = call("POST", base("linkedin") + "/rest/posts", _linkedin_headers(cs), body, want_headers=True)
    pid = headers.get("x-restli-id") or headers.get("X-RestLi-Id") or ""
    return f"Posted on LinkedIn: https://www.linkedin.com/feed/update/{pid}" if pid else "Posted on LinkedIn."


def webhook_post(cs: dict, company_name: str, platform: str, text: str, link: str = "") -> str:
    data = call("POST", cs["webhook_url"].strip(), None,
                {"type": "social_post", "company": company_name, "platform": platform, "content": text, "link": link, "text": text})
    return "Sent to the posting webhook" + (f": {data.get('url')}" if isinstance(data, dict) and data.get("url") else ".")


def _with_link(text: str, link: str, limit: int) -> str:
    text = (text or "").strip()
    if link and link not in text:
        text = f"{text}\n{link}" if text else link
    if len(text) > limit:
        raise ConnectorError(f"The post is {len(text)} characters; this platform allows {limit}. Shorten it and resubmit.")
    return text


SOCIAL_PLATFORMS = {"x": "x", "twitter": "x", "bluesky": "bluesky", "bsky": "bluesky", "mastodon": "mastodon", "fediverse": "mastodon",
                    "linkedin": "linkedin"}


def social_provider(cs: dict, platform: str) -> str | None:
    """Which connector publishes to this platform: a direct one, the webhook, or none."""
    key = SOCIAL_PLATFORMS.get((platform or "").lower().strip())
    if key and configured(cs, key):
        return key
    if configured(cs, "webhook"):
        return "webhook"
    return None


def social_post(cs: dict, company_name: str, platform: str, text: str, link: str = "") -> str:
    p = social_provider(cs, platform)
    if p == "x":
        return x_post(cs, text, link)
    if p == "bluesky":
        return bluesky_post(cs, text, link)
    if p == "mastodon":
        return mastodon_post(cs, text, link)
    if p == "linkedin":
        return linkedin_post(cs, text, link)
    if p == "webhook":
        return webhook_post(cs, company_name, platform, text, link)
    raise ConnectorError(f"No connector can post to {platform}.")


# ---------------------------------------------------------------- scheduling: Cal.com

def calcom_bookings(cs: dict, limit: int = 20) -> list[dict]:
    data = call("GET", base("calcom", cs.get("calcom_url")) + f"/v2/bookings{_qs({'status': 'upcoming', 'take': max(1, min(int(limit or 20), 100))})}",
                {"Authorization": f"Bearer {cs['calcom_api_key'].strip()}", "cal-api-version": CALCOM_VERSION})
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("bookings") or []
    out = []
    for b in rows or []:
        people = b.get("attendees") or []
        out.append({"id": b.get("uid") or b.get("id"), "title": b.get("title") or "", "start": b.get("start") or b.get("startTime") or "",
                    "end": b.get("end") or b.get("endTime") or "", "status": b.get("status") or "",
                    "attendees": [f"{a.get('name') or ''} <{a.get('email') or ''}>".strip() for a in people],
                    "location": b.get("location") or b.get("meetingUrl") or ""})
    return out


# ---------------------------------------------------------------- domains: RDAP

def domain_check(domain: str) -> dict:
    d = (domain or "").strip().lower().rstrip(".")
    d = re.sub(r"^https?://", "", d).split("/")[0]
    if not re.fullmatch(r"(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", d):
        raise ConnectorError("Give a domain name like example.com.")
    status_code, text, _ = _http("GET", f"{base('rdap')}/domain/{d}", {"Accept": "application/rdap+json, application/json"})
    if status_code == 404:
        return {"domain": d, "registered": False, "note": "No registration found in RDAP; it's probably available. Confirm at a registrar before relying on it."}
    if status_code != 200:
        return {"domain": d, "registered": None, "note": f"RDAP couldn't answer for this domain (HTTP {status_code}); check with a registrar."}
    try:
        data = json.loads(text)
    except ValueError:
        return {"domain": d, "registered": None, "note": "RDAP returned something unreadable; check with a registrar."}
    events = {e.get("eventAction"): str(e.get("eventDate") or "")[:10] for e in data.get("events") or [] if isinstance(e, dict)}
    registrar = ""
    for ent in data.get("entities") or []:
        if "registrar" in (ent.get("roles") or []):
            for item in (ent.get("vcardArray") or [None, []])[1] or []:
                if isinstance(item, list) and item and item[0] == "fn":
                    registrar = str(item[-1])
    return {"domain": d, "registered": True, "registrar": registrar, "registered_on": events.get("registration", ""),
            "expires": events.get("expiration", ""), "status": [s for s in data.get("status") or [] if isinstance(s, str)][:6],
            "note": "Registered."}


# ---------------------------------------------------------------- owner notifications

def notify_targets(s: dict) -> list[str]:
    out = []
    if (s.get("notify_webhook_url") or "").strip():
        out.append("webhook")
    if (s.get("telegram_bot_token") or "").strip() and str(s.get("telegram_chat_id") or "").strip():
        out.append("telegram")
    if (s.get("ntfy_topic") or "").strip():
        out.append("ntfy")
    return out


def deliver(s: dict, title: str, text: str) -> list[dict]:
    """Send one notification to every configured channel. Returns a result per channel."""
    results = []
    title = " ".join((title or "Holdco").split())[:120]
    text = (text or "").strip()[:1500]
    url = (s.get("notify_webhook_url") or "").strip()
    if url:
        kind = url.lower()
        if "discord" in kind:
            payload = {"content": f"**{title}**\n{text}"[:1900]}
        elif "slack" in kind:
            payload = {"text": f"*{title}*\n{text}"}
        else:
            payload = {"title": title, "text": text, "content": f"{title}\n{text}", "source": "holdco"}
        try:
            call("POST", url, None, payload)
            results.append({"channel": "webhook", "ok": True, "message": "sent"})
        except ConnectorError as e:
            results.append({"channel": "webhook", "ok": False, "message": str(e)})
    tok, chat = (s.get("telegram_bot_token") or "").strip(), str(s.get("telegram_chat_id") or "").strip()
    if tok and chat:
        try:
            call("POST", f"{base('telegram')}/bot{tok}/sendMessage", None,
                 {"chat_id": chat, "text": f"{title}\n{text}", "disable_web_page_preview": True})
            results.append({"channel": "telegram", "ok": True, "message": "sent"})
        except ConnectorError as e:
            results.append({"channel": "telegram", "ok": False, "message": str(e)})
    topic = (s.get("ntfy_topic") or "").strip()
    if topic:
        turl = topic if topic.startswith(("http://", "https://")) else f"{base('ntfy')}/{topic.strip('/')}"
        try:
            call("POST", turl, {"Title": title.encode("ascii", "ignore").decode() or "Holdco", "Content-Type": "text/plain; charset=utf-8"},
                 raw=text.encode("utf-8"))
            results.append({"channel": "ntfy", "ok": True, "message": "sent"})
        except ConnectorError as e:
            results.append({"channel": "ntfy", "ok": False, "message": str(e)})
    return results


def notify(kind: str, title: str, text: str) -> None:
    """Ping the owner in the background. kind: approvals | messages | errors."""
    try:
        s = db.get_settings()
    except Exception:
        return
    if kind not in (s.get("notify_events") or []) or not notify_targets(s):
        return

    def go():
        for r in deliver(s, title, text):
            if not r["ok"]:
                print(f"[notify] {r['channel']} failed: {r['message']}", file=sys.stderr)

    threading.Thread(target=go, daemon=True, name="notify").start()


# ---------------------------------------------------------------- connection tests

def test(key: str, cs: dict, extra: dict | None = None) -> tuple[str, dict]:
    """Try the connector. Returns (message, settings to store) or raises ConnectorError."""
    extra = extra or {}
    if key not in config.CONNECTORS:
        raise ConnectorError(f"Unknown connector {key}.")
    if not configured(cs, key):
        missing = [f["label"] for f in config.CONNECTORS[key]["fields"] if f["key"] in config.CONNECTORS[key]["required"] and not cs.get(f["key"])]
        raise ConnectorError("Fill in " + ", ".join(missing) + " first.")
    if key == "email":
        from . import emailer
        company = extra.get("company") or {"id": 0, "name": "Holdco", "settings": json.dumps(cs)}
        return emailer.test(company, extra.get("to", "")), {}
    if key == "netlify":
        me = call("GET", f"{base('netlify')}/api/v1/user", _netlify_headers(cs))
        msg = f"Connected to Netlify as {me.get('email') or me.get('full_name') or 'your account'}."
        if cs.get("netlify_site_id"):
            site = call("GET", f"{base('netlify')}/api/v1/sites/{cs['netlify_site_id']}", _netlify_headers(cs))
            msg += f" Site: {site.get('ssl_url') or site.get('url') or site.get('name')}."
            return msg, {"site_url": site.get("ssl_url") or site.get("url") or cs.get("site_url", "")}
        return msg + " The site is created on the first approved deploy.", {}
    if key == "stripe":
        s = stripe_summary(cs, 30)
        return (f"Connected to Stripe. Balance {_money(s['balance_available'], s['currency'])} available; {s['sales_count']} sales in "
                f"the last 30 days; {s['subscriptions_active']} active subscriptions."), {}
    if key == "lemonsqueezy":
        me = (call("GET", base("lemonsqueezy") + "/v1/users/me", {"Authorization": f"Bearer {cs['lemonsqueezy_api_key'].strip()}",
                                                                   "Accept": "application/vnd.api+json"}).get("data") or {}).get("attributes") or {}
        return f"Connected to Lemon Squeezy as {me.get('name') or me.get('email') or 'your account'}.", {}
    if key == "gumroad":
        me = _gumroad(cs, "/v2/user").get("user") or {}
        return f"Connected to Gumroad as {me.get('name') or me.get('email') or 'your account'}.", {}
    if key == "plausible":
        s = plausible_stats(cs, "7d")
        return f"Connected to Plausible: {s['visitors']} visitors in the last 7 days.", {}
    if key == "goatcounter":
        call("GET", base("goatcounter", cs["goatcounter_url"]) + "/api/v0/me", {"Authorization": f"Bearer {cs['goatcounter_api_key'].strip()}"})
        s = goatcounter_stats(cs, "7d")
        return f"Connected to GoatCounter: {s['visitors']} visitors in the last 7 days.", {}
    if key == "x":
        me = _x_me(cs)
        return f"Connected to X as @{me.get('username', '?')}.", {}
    if key == "bluesky":
        sess = _bluesky_session(cs)
        return f"Connected to Bluesky as @{sess.get('handle', cs['bluesky_handle'])}.", {}
    if key == "mastodon":
        me = call("GET", base("mastodon", cs["mastodon_url"]) + "/api/v1/accounts/verify_credentials",
                  {"Authorization": f"Bearer {cs['mastodon_access_token'].strip()}"})
        return f"Connected to Mastodon as @{me.get('username', '?')}.", {}
    if key == "linkedin":
        urn = linkedin_author(cs)
        return f"Connected to LinkedIn ({urn}).", {"linkedin_author_urn": urn}
    if key == "webhook":
        call("POST", cs["webhook_url"].strip(), None, {"type": "test", "content": "Holdco can reach this webhook.", "text": "Holdco can reach this webhook."})
        return "The webhook accepted a test message.", {}
    if key == "calcom":
        me = call("GET", base("calcom", cs.get("calcom_url")) + "/v2/me",
                  {"Authorization": f"Bearer {cs['calcom_api_key'].strip()}", "cal-api-version": CALCOM_VERSION})
        who = (me.get("data") or {}) if isinstance(me, dict) else {}
        return f"Connected to Cal.com as {who.get('username') or who.get('email') or 'your account'}.", {}
    raise ConnectorError(f"No test for {key}.")
