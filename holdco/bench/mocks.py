"""The scripted outside world for Holdco Bench and the test suite (Phase 3, docs/phases/phase-3-verify.md section 1).

From Phase 3 this module is the one home of the fake services: Stripe, Telegram (with an ntfy endpoint) and an MCP
server over stdio or Streamable HTTP. tests/fake_stripe.py, tests/fake_telegram.py and tests/fake_mcp_server.py
re-export them, so the behaviours the suite tests are the ones the Bench runs against. The module is stdlib only and
imports no other Holdco module at load time: the stdio MCP server runs it in a subprocess
(python tests/fake_mcp_server.py --stdio).

install(spec, company) wires one Bench scenario's mocks into the running process and returns a Mocks handle; its
remove() puts everything back, also after a crash. A spec (Scenario.mocks) may hold:

- search: "blocked" (every provider fails with a bot challenge, and the Doctor's search row says so) or a list of
  {title, url, snippet} returned for any query. Without it a search finds nothing.
- fetch: {url: page text or HTML, or an HTTP status such as 404}. web.http_get answers from it, so fetch_url,
  http_request and an external browser_check read these pages; any other address is a 404. Nothing reaches the
  network.
- stripe: true starts the fake Stripe and connects the bench company to it with a test key.
- mcp: true starts the fake MCP server (HTTP) and adds it as the bench company's "benchmcp" connector.
- telegram: true starts the fake Telegram; what would reach the owner's phone goes there instead.
- playwright: "absent" (the default: the static browser check, reading the page from disk) or the scripted text
  browser_check returns ({path: text} or one text for every page).
- write_file_fail_once: the first write_file fails with a busy disk.
- connector_down: a connector key (such as "resend") the bench company has, whose last health check failed.

Every patch answers only in the thread that installed it (the Bench runs one scenario at a time in one thread). Any
other thread, such as the dashboard, the scheduler or a chat with a real company while a bench runs from Settings,
gets the original function, so a mock never reaches a real company. In the bench thread the owner's settings are
overlaid with the scenario's, the bench company sees only the connectors its scenario sets up, nothing goes to the
owner's phone, and the model's replies and the tool calls are recorded for scoring.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ======================================================================== fake Stripe
# A fake Stripe API, so the Stripe connector and the Phase 1 gate run without network or a real account.
#
#     stripe = FakeStripe().start()
#     ... create payment links through the connector ...
#     stripe.add_charge(amount=1900, currency="usd", paid=True)   # a test purchase
#     stripe.add_subscription(unit_amount=12000, interval="year") # for MRR
#     stripe.stop()
#     python tests/fake_stripe.py 12001   # standalone, prints the URL
#
# Auth: `Authorization: Bearer <key>`; a key containing "_test_" is accepted, any other key gets
# 401 {"error": {"message": "Invalid API Key"}}. Bodies are form-encoded; bracket keys are unflattened
# (line_items[0][price] -> {"line_items": [{"price": ...}]}).
#
# Endpoints: GET /v1/balance; POST /v1/products, GET /v1/products[/<id>]; POST /v1/prices, GET /v1/prices/<id>;
# POST/GET /v1/payment_links, GET /v1/payment_links/<id>[/line_items]; GET /pay/<id> (an HTML checkout page with a
# title, an h1 with the product name and a <form>, no auth); GET /v1/charges (created[gte], created[gt], limit,
# starting_after); GET /v1/charges/<id> and GET /v1/payment_intents/<id> (from the stored charges); GET
# /v1/customers; GET /v1/subscriptions (status filter); POST /v1/refunds. Test hooks (no auth):
# POST /_test/charges, /_test/customers, /_test/subscriptions with a JSON body, or the Python methods add_charge,
# add_customer, add_subscription.
#
# Records: requests ({"method", "path", "query", "body"} for every API call), plus the object stores products,
# prices, links, charges, customers, subscriptions and refunds.

def unflatten(pairs: list[tuple[str, str]]) -> dict:
    """Stripe's bracket syntax back into nested dicts and lists: a[b][0][c]=v -> {"a": {"b": [{"c": "v"}]}};
    expand[]=x -> {"expand": ["x"]}."""
    root: dict = {}
    for key, value in pairs:
        parts = re.findall(r"[^\[\]]+|\[\]", key)
        if not parts:
            continue
        node = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if part == "[]":
                continue
            nxt = parts[i + 1] if not last else None
            if last:
                node[part] = value
            elif nxt == "[]":
                node.setdefault(part, {"__list__": []})
                node[part]["__list__"].append(value)
                break
            else:
                node = node.setdefault(part, {})
    return _lists(root)


def _lists(node):
    if isinstance(node, dict):
        if set(node) == {"__list__"}:
            return list(node["__list__"])
        if node and all(k.isdigit() for k in node):
            return [_lists(node[k]) for k in sorted(node, key=int)]
        return {k: _lists(v) for k, v in node.items()}
    return node


class FakeStripe:
    def __init__(self, port: int = 0):
        self.requests: list[dict] = []
        self.products: dict[str, dict] = {}
        self.prices: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.charges: list[dict] = []
        self.customers: list[dict] = []
        self.subscriptions: list[dict] = []
        self.refunds: list[dict] = []
        self._lock = threading.RLock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _error(self, code: int, message: str, kind: str = "invalid_request_error"):
                self._json(code, {"error": {"type": kind, "message": message}})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

            def _route(self, method: str):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                query = unflatten(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
                if path.startswith("/pay/") and method == "GET":
                    return outer._pay_page(self, path[len("/pay/"):])
                if path.startswith("/_test/") and method == "POST":
                    try:
                        body = json.loads(raw or "{}")
                    except ValueError:
                        return self._error(400, "send JSON")
                    hook = {"charges": outer.add_charge, "customers": outer.add_customer,
                            "subscriptions": outer.add_subscription}.get(path[len("/_test/"):])
                    if not hook:
                        return self._error(404, "Unrecognized test hook")
                    return self._json(200, hook(**body))
                auth = self.headers.get("Authorization") or ""
                key = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if "_test_" not in key:
                    return self._json(401, {"error": {"type": "invalid_request_error",
                                                      "message": "Invalid API Key"}})
                if raw and "json" in (self.headers.get("Content-Type") or ""):
                    return self._error(400, "Stripe expects form-encoded bodies")
                body = unflatten(urllib.parse.parse_qsl(raw, keep_blank_values=True))
                with outer._lock:
                    outer.requests.append({"method": method, "path": path, "query": query, "body": body,
                                           "idempotency_key": self.headers.get("Idempotency-Key")})
                    code, obj = outer._api(method, path, query, body)
                self._json(code, obj)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    # ---- object helpers

    def _new_id(self, prefix: str, store) -> str:
        """prefix_<n> numbered within its own kind, so the first payment link is plink_1."""
        return f"{prefix}_{len(store) + 1}"

    @staticmethod
    def _list(items: list, query: dict) -> dict:
        try:
            limit = max(1, min(int(query.get("limit") or 10), 100))
        except (TypeError, ValueError):
            limit = 10
        after = query.get("starting_after")
        if after:
            ids = [x["id"] for x in items]
            items = items[ids.index(after) + 1:] if after in ids else []
        return {"object": "list", "data": items[:limit], "has_more": len(items) > limit, "url": ""}

    def _api(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        now = int(time.time())
        if path == "/v1/balance" and method == "GET":
            return 200, {"object": "balance", "available": [{"amount": 0, "currency": "usd"}],
                         "pending": [{"amount": 0, "currency": "usd"}], "livemode": False}
        if path == "/v1/products" and method == "POST":
            if not body.get("name"):
                return 400, {"error": {"message": "Missing required param: name."}}
            if "description" in body and body["description"] == "":
                return 400, {"error": {"message": "You passed an empty string for 'description'."}}
            pid = self._new_id("prod", self.products)
            self.products[pid] = {"id": pid, "object": "product", "name": body["name"],
                                  "description": body.get("description"), "active": True, "created": now,
                                  "livemode": False}
            return 200, self.products[pid]
        if path == "/v1/products" and method == "GET":
            return 200, self._list(list(reversed(self.products.values())), query)
        m = re.match(r"^/v1/products/([^/]+)$", path)
        if m and method == "GET":
            p = self.products.get(m.group(1))
            return (200, p) if p else (404, {"error": {"message": f"No such product: '{m.group(1)}'"}})
        if path == "/v1/prices" and method == "POST":
            if body.get("product") not in self.products:
                return 400, {"error": {"message": f"No such product: '{body.get('product')}'"}}
            try:
                unit = int(body.get("unit_amount"))
            except (TypeError, ValueError):
                return 400, {"error": {"message": "Invalid integer: unit_amount"}}
            recurring = body.get("recurring") or None
            if recurring:
                recurring = {"interval": recurring.get("interval"),
                             "interval_count": int(recurring.get("interval_count") or 1)}
            pid = self._new_id("price", self.prices)
            self.prices[pid] = {"id": pid, "object": "price", "unit_amount": unit,
                                "currency": (body.get("currency") or "usd").lower(), "product": body["product"],
                                "recurring": recurring, "type": "recurring" if recurring else "one_time",
                                "active": True, "created": now, "livemode": False}
            return 200, self.prices[pid]
        m = re.match(r"^/v1/prices/([^/]+)$", path)
        if m and method == "GET":
            p = self.prices.get(m.group(1))
            return (200, p) if p else (404, {"error": {"message": f"No such price: '{m.group(1)}'"}})
        if path == "/v1/payment_links" and method == "POST":
            items = body.get("line_items") or []
            if not items:
                return 400, {"error": {"message": "Missing required param: line_items."}}
            data = []
            for i, it in enumerate(items):
                price = self.prices.get(it.get("price"))
                if not price:
                    return 400, {"error": {"message": f"No such price: '{it.get('price')}'"}}
                qty = int(it.get("quantity") or 1)
                data.append({"id": f"li_{len(self.links) + 1}_{i}", "object": "item",
                             "description": self.products[price["product"]]["name"], "price": price,
                             "quantity": qty, "amount_total": price["unit_amount"] * qty,
                             "currency": price["currency"]})
            lid = self._new_id("plink", self.links)
            self.links[lid] = {"id": lid, "object": "payment_link", "active": True, "url": f"{self.url}/pay/{lid}",
                               "created": now, "livemode": False,
                               "line_items": {"object": "list", "data": data, "has_more": False}}
            return 200, self.links[lid]
        if path == "/v1/payment_links" and method == "GET":
            return 200, self._list(list(reversed(self.links.values())), query)
        m = re.match(r"^/v1/payment_links/([^/]+)(/line_items)?$", path)
        if m and method == "GET":
            link = self.links.get(m.group(1))
            if not link:
                return 404, {"error": {"message": f"No such payment link: '{m.group(1)}'"}}
            return 200, (link["line_items"] if m.group(2) else link)
        if path == "/v1/charges" and method == "GET":
            created = query.get("created") if isinstance(query.get("created"), dict) else {}
            items = list(reversed(self.charges))
            if created.get("gte"):
                items = [c for c in items if c["created"] >= int(created["gte"])]
            if created.get("gt"):
                items = [c for c in items if c["created"] > int(created["gt"])]
            return 200, self._list(items, query)
        m = re.match(r"^/v1/(charges|payment_intents)/([^/]+)$", path)
        if m and method == "GET":   # one payment, as a refund looks up its currency
            field = "id" if m.group(1) == "charges" else "payment_intent"
            charge = next((c for c in self.charges if c[field] == m.group(2)), None)
            if not charge:
                return 404, {"error": {"message": f"No such {m.group(1)[:-1]}: '{m.group(2)}'"}}
            if m.group(1) == "charges":
                return 200, charge
            return 200, {"id": charge["payment_intent"], "object": "payment_intent", "amount": charge["amount"],
                         "currency": charge["currency"], "status": "succeeded", "latest_charge": charge["id"],
                         "livemode": False}
        if path == "/v1/customers" and method == "GET":
            return 200, self._list(list(reversed(self.customers)), query)
        if path == "/v1/subscriptions" and method == "GET":
            items = list(reversed(self.subscriptions))
            status = query.get("status")
            if status and status != "all":
                items = [s for s in items if s["status"] == status]
            elif not status:
                items = [s for s in items if s["status"] != "canceled"]
            return 200, self._list(items, query)
        if path == "/v1/refunds" and method == "POST":
            ref = body.get("payment_intent") or body.get("charge")
            charge = next((c for c in self.charges if ref in (c["payment_intent"], c["id"])), None)
            if not charge:
                return 400, {"error": {"message": f"No such payment_intent: '{ref}'"}}
            amount = int(body.get("amount") or charge["amount"] - charge["amount_refunded"])
            if amount > charge["amount"] - charge["amount_refunded"]:
                return 400, {"error": {"message": "Refund amount is greater than the unrefunded amount."}}
            charge["amount_refunded"] += amount
            charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
            rid = self._new_id("re", self.refunds)
            refund = {"id": rid, "object": "refund", "amount": amount, "currency": charge["currency"],
                      "payment_intent": charge["payment_intent"], "charge": charge["id"], "status": "succeeded",
                      "created": now}
            self.refunds.append(refund)
            return 200, refund
        return 404, {"error": {"message": f"Unrecognized request URL ({method}: {path})."}}

    def _pay_page(self, handler, lid: str):
        link = self.links.get(urllib.parse.unquote(lid))
        if not link:
            return handler._send(404, b"<!doctype html><title>Not found</title><h1>Not found</h1>",
                                 "text/html; charset=utf-8")
        item = link["line_items"]["data"][0]
        price = item["price"]
        per = f" per {price['recurring']['interval']}" if price.get("recurring") else ""
        name = html.escape(item["description"])
        page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{name}: checkout (test mode)'
                f'</title></head><body><h1>{name}</h1><p>${price["unit_amount"] / 100:.2f}{per}</p>'
                f'<form action="/pay/{html.escape(lid)}/confirm" method="post"><label>Email '
                f'<input type="email" name="email"></label><label>Card number <input name="card"></label>'
                f'<button type="submit">Pay</button></form></body></html>')
        handler._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    # ---- test hooks

    def add_charge(self, amount: int, currency: str = "usd", paid: bool = True, created: int | None = None,
                   customer_email: str | None = None, amount_refunded: int = 0) -> dict:
        """A test purchase: a succeeded charge (failed when paid is false), with its own payment intent."""
        with self._lock:
            n = len(self.charges) + 1
            customer = None
            if customer_email:
                customer = next((c["id"] for c in self.customers if c.get("email") == customer_email), None)
                customer = customer or self.add_customer(email=customer_email)["id"]
            charge = {"id": f"ch_{n}", "object": "charge", "amount": int(amount),
                      "amount_refunded": int(amount_refunded), "currency": currency.lower(), "paid": bool(paid),
                      "status": "succeeded" if paid else "failed", "refunded": False,
                      "created": int(created if created is not None else time.time()), "payment_intent": f"pi_{n}",
                      "customer": customer, "receipt_email": customer_email,
                      "billing_details": {"email": customer_email}, "livemode": False}
            self.charges.append(charge)
            return charge

    def add_customer(self, email: str = "", name: str = "") -> dict:
        with self._lock:
            cus = {"id": f"cus_{len(self.customers) + 1}", "object": "customer", "email": email or None,
                   "name": name or None, "created": int(time.time()), "livemode": False}
            self.customers.append(cus)
            return cus

    def add_subscription(self, unit_amount: int, interval: str = "month", quantity: int = 1, currency: str = "usd",
                         status: str = "active", interval_count: int = 1) -> dict:
        with self._lock:
            n = len(self.subscriptions) + 1
            price = {"id": f"price_sub_{n}", "object": "price", "unit_amount": int(unit_amount),
                     "currency": currency.lower(), "type": "recurring",
                     "recurring": {"interval": interval, "interval_count": int(interval_count)}}
            sub = {"id": f"sub_{n}", "object": "subscription", "status": status, "created": int(time.time()),
                   "items": {"object": "list", "data": [{"id": f"si_{n}", "object": "subscription_item",
                                                         "price": price, "quantity": int(quantity)}]},
                   "livemode": False}
            self.subscriptions.append(sub)
            return sub

    def clear(self) -> None:
        with self._lock:
            for store in (self.requests, self.charges, self.customers, self.subscriptions, self.refunds):
                store.clear()
            for store in (self.products, self.prices, self.links):
                store.clear()

    def posts(self, path: str | None = None) -> list[dict]:
        """The POSTs received, optionally only those to one path."""
        return [r for r in self.requests if r["method"] == "POST" and (path is None or r["path"] == path)]

    # ---- lifecycle

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()




# ======================================================================== fake Telegram
# A fake Telegram Bot API (plus a fake ntfy endpoint) so the phone side of Holdco runs without network: a
# ThreadingHTTPServer on 127.0.0.1 that records what Holdco sends and lets a test queue updates (button presses, text
# commands) for the next getUpdates call.
#
#     tg = FakeTelegram().start()          # tg.url is the telegram_api_base, tg.token the bot token, tg.chat_id 42
#     tg.push_callback("a:12:approve", message_id=tg.sent[-1]["message_id"])
#     tg.push_message("/status")
#     ...; tg.stop()
#     python tests/fake_telegram.py 11998  # standalone, prints the URL
#
# Records: sent (sendMessage bodies with the message_id assigned), edits (editMessageReplyMarkup and
# editMessageText bodies, with "method"), answered (answerCallbackQuery bodies), ntfy ({"topic", "body", "title",
# "priority"}), requests (every Bot API call: {"method", "body"}). `edited` and `push_text` are aliases.
# Set `delay` (seconds) to make sendMessage answer slowly, for tests that prove the hooks never wait on the phone.

MAX_WAIT = 0.5   # the long poll returns after at most this long when nothing is queued, so tests stay fast


class FakeTelegram:
    def __init__(self, port: int = 0, chat_id: int = 42, token: str = "test-token"):
        self.token = token
        self.chat_id = chat_id
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answered: list[dict] = []
        self.ntfy: list[dict] = []
        self.requests: list[dict] = []
        self.delay = 0.0   # seconds sendMessage sleeps before answering (a slow phone network, for the hook tests)
        self._updates: list[dict] = []
        self._cv = threading.Condition()
        self._next_update = 1000
        self._next_message = 100
        self._next_cq = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _raw(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                self._route()

            def do_POST(self):
                self._route()

            def _route(self):
                parsed = urllib.parse.urlparse(self.path)
                raw = self._raw()
                if parsed.path.startswith("/ntfy/"):
                    topic = parsed.path[len("/ntfy/"):]
                    outer.ntfy.append({"topic": topic, "body": raw.decode("utf-8", "replace"),
                                       "title": self.headers.get("Title"), "priority": self.headers.get("Priority")})
                    return self._json(200, {"id": "x", "topic": topic, "time": int(time.time())})
                mt = re.match(r"^/bot([^/]+)/(\w+)$", parsed.path)
                if not mt:
                    return self._json(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                token, method = mt.groups()
                if token != outer.token:
                    return self._json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                body: dict = {}
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
                for k, v in urllib.parse.parse_qs(parsed.query).items():
                    body.setdefault(k, v[0])
                outer.requests.append({"method": method, "body": body})
                if method == "getMe":
                    return self._json(200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Holdco",
                                                                    "username": "holdco_test_bot"}})
                if method == "getUpdates":
                    return self._json(200, {"ok": True, "result": outer._poll(body)})
                if method == "sendMessage":
                    if outer.delay:
                        time.sleep(outer.delay)
                    if not str(body.get("text") or "").strip():
                        return self._json(400, {"ok": False, "error_code": 400,
                                                "description": "Bad Request: message text is empty"})
                    mid = outer._message_id()
                    outer.sent.append({**body, "message_id": mid})
                    result = {"message_id": mid, "chat": {"id": body.get("chat_id")}, "text": body.get("text")}
                    return self._json(200, {"ok": True, "result": result})
                if method == "answerCallbackQuery":
                    outer.answered.append(dict(body))
                    return self._json(200, {"ok": True, "result": True})
                if method in ("editMessageReplyMarkup", "editMessageText"):
                    outer.edits.append({**body, "method": method})
                    return self._json(200, {"ok": True, "result": {"message_id": body.get("message_id")}})
                return self._json(404, {"ok": False, "error_code": 404, "description": "Not Found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    # ---- test hooks

    @property
    def edited(self) -> list[dict]:
        return self.edits

    def clear(self) -> None:
        """Forget every record and queued update (between tests)."""
        with self._cv:
            self.sent.clear()
            self.edits.clear()
            self.answered.clear()
            self.ntfy.clear()
            self.requests.clear()
            self._updates.clear()

    def push_callback(self, data: str, message_id: int | None = 1, chat_id=None) -> int:
        """Queue a button press on one of the sent messages (its text is looked up so edits have something to keep)."""
        chat = self.chat_id if chat_id is None else chat_id
        text = next((m.get("text", "") for m in self.sent if m.get("message_id") == message_id), "")
        self._next_cq += 1
        return self._push({"callback_query": {
            "id": f"cq-{self._next_cq}", "from": {"id": chat, "is_bot": False, "first_name": "Owner"},
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}, "text": text},
            "chat_instance": "1", "data": data}})

    def push_message(self, text: str, chat_id=None) -> int:
        """Queue a text message (a command like /status) from the owner's chat unless chat_id says otherwise."""
        chat = self.chat_id if chat_id is None else chat_id
        return self._push({"message": {"message_id": self._message_id(), "from": {"id": chat, "is_bot": False},
                                       "chat": {"id": chat, "type": "private"}, "date": int(time.time()),
                                       "text": text}})

    push_text = push_message

    def _push(self, update: dict) -> int:
        with self._cv:
            uid = self._next_update
            self._next_update += 1
            self._updates.append({"update_id": uid, **update})
            self._cv.notify_all()
        return uid

    def _message_id(self) -> int:
        with self._cv:
            self._next_message += 1
            return self._next_message

    def _poll(self, body: dict) -> list[dict]:
        """getUpdates: confirm (drop) updates below offset, then wait briefly for one at or above it."""
        try:
            offset = int(body.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            timeout = min(float(body.get("timeout") or 0), MAX_WAIT)
        except (TypeError, ValueError):
            timeout = 0
        deadline = time.time() + timeout
        with self._cv:
            self._updates[:] = [u for u in self._updates if u["update_id"] >= offset]
            while not self._updates:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            return [json.loads(json.dumps(u)) for u in self._updates]

    # ---- lifecycle

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()




# ======================================================================== fake MCP server
# A fake MCP server: both protocol generations, over stdio or Streamable HTTP.
#
#     fake = FakeMCPHttp(port=0, old=False, sse=False, auth_base=None).start()   # fake.url, fake.mcp_url
#     ...; fake.stop()
#     python tests/fake_mcp_server.py --stdio [--old]
#     python tests/fake_mcp_server.py --http PORT [--old] [--sse] [--auth URL]
#
# Tools: echo(text), add(a, b) (structuredContent {"sum": n} and text), fail(text) (isError), image() (a 1x1 PNG
# block), slow(seconds), input_required() (a result whose resultType is "input_required").
#
# New mode (2026-07-28, the default): server/discover answers; every request must carry the namespaced _meta keys
# protocolVersion and clientCapabilities and, over HTTP, the Mcp-Method header (the JSON-RPC method) and, on a request
# that targets a name (tools/call), the Mcp-Name header with that name, else -32600; a version not in `supported`
# gets -32022; results carry resultType "complete" and _meta serverInfo; initialize and ping answer -32601; no
# session id. Old mode (--old, 2025-11-25): server/discover answers -32601; initialize answers `old_version`;
# tools/* before notifications/initialized answer -32600 "not initialized"; ping answers {}; over HTTP initialize
# sets Mcp-Session-Id, later requests without it get 400 and with an unknown one 404.
#
# HTTP: POST /mcp (any POST path except the well-known ones is the endpoint). With sse every reply is one
# "event: message" / "data: <json>" block. With auth_base, requests need a bearer token that
# GET <auth_base>/introspect?token= calls active, else 401 with WWW-Authenticate pointing at
# <self>/.well-known/oauth-protected-resource, which names auth_base.
#
# Records: `calls` (every JSON-RPC message received: a (method, params) pair that also answers ["headers"], ["id"],
# .headers and .id), `requests` (every HTTP request: method, path, status, headers). Test hooks on FakeMCPHttp:
# `supported`, `old_version`, `break_streams` (the next n SSE replies end before the reply), `challenge` (replaces
# the WWW-Authenticate value), `discover_status` (answer server/discover with that HTTP status), `expire_sessions()`.
# The stdio script exits with "fake-mcp: fatal error ..." on stderr when the tool named in FAKE_MCP_CRASH_ON is called.

CURRENT = "2026-07-28"
OLD = "2025-11-25"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"

# A 1x1 transparent PNG.
PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": _obj({"text": {"type": "string", "description": "Text to echo"}}, ["text"])},
    {"name": "add", "description": "Add two numbers and return the sum.",
     "inputSchema": _obj({"a": {"type": "number"}, "b": {"type": "number"}}, ["a", "b"])},
    {"name": "fail", "description": "Always fail with the given text.",
     "inputSchema": _obj({"text": {"type": "string"}}, ["text"])},
    {"name": "image", "description": "Return a tiny PNG image.", "inputSchema": _obj({})},
    {"name": "slow", "description": "Wait the given number of seconds, then answer.",
     "inputSchema": _obj({"seconds": {"type": "number"}}, ["seconds"])},
    {"name": "input_required", "description": "Ask the client for more input before answering.",
     "inputSchema": _obj({})},
]


class Headers(dict):
    """Request headers with lower-case keys; get() and [] ignore case."""

    def __init__(self, items=()):
        super().__init__((str(k).lower(), v) for k, v in dict(items).items())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


class Call(tuple):
    """One received JSON-RPC message: (method, params), plus .headers and .id (also as ["headers"], ["id"])."""

    def __new__(cls, method, params, headers=None, rid=None):
        self = super().__new__(cls, (method, params))
        self.headers = headers if headers is not None else Headers()
        self.id = rid
        return self

    @property
    def method(self):
        return self[0]

    @property
    def params(self):
        return self[1]

    def __getitem__(self, key):
        if isinstance(key, str):
            return {"method": self[0], "params": self[1], "headers": self.headers, "id": self.id}[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def _error(rid, code, message, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _result(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text(text) -> dict:
    return {"type": "text", "text": str(text)}


class FakeMCPCore:
    """The protocol, shared by the stdio script and the HTTP server."""

    def __init__(self, old: bool = False):
        self.old = old
        self.calls: list[Call] = []
        self.supported = [CURRENT]       # versions the new mode accepts; anything else gets -32022
        self.old_version = OLD           # what initialize answers in old mode
        self.server_info = {"name": "fake-mcp", "version": "1.0.0"}
        self.initialized: set = set()    # sessions that sent notifications/initialized (old mode)
        self.stopping = threading.Event()   # wakes slow() when the server stops
        self.crash_on: str | None = None    # stdio script only: exit hard when this tool is called

    def handle(self, msg, headers: Headers | None = None, session=None) -> dict | None:
        """A reply for a request, None for a notification. `headers` is None over stdio."""
        if not isinstance(msg, dict) or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None   # a client's answer to a request of ours
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
        method = str(msg["method"])
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        rid = msg.get("id")
        self.calls.append(Call(method, params, headers, rid))
        return self._old(method, params, rid, "id" in msg, session) if self.old else \
            self._new(method, params, rid, "id" in msg, headers)

    # -- the current generation --

    def _new(self, method, params, rid, is_request, headers) -> dict | None:
        if not is_request:
            return None
        if method in ("initialize", "ping"):
            return _error(rid, -32601, f"Method not found: {method}")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        if META_VERSION not in meta or META_CAPABILITIES not in meta:
            return _error(rid, -32600, "Missing _meta protocolVersion or clientCapabilities")
        if headers is not None:
            if headers.get("Mcp-Method") != method:
                return _error(rid, -32600, "Missing or wrong Mcp-Method header")
            if method == "tools/call" and headers.get("Mcp-Name") != str(params.get("name")):
                return _error(rid, -32600, "Missing or wrong Mcp-Name header")
        if meta.get(META_VERSION) not in self.supported:
            return _error(rid, -32022, "Unsupported protocol version",
                          {"supported": list(self.supported), "requested": meta.get(META_VERSION)})
        if method == "server/discover":
            result = {"protocolVersions": list(self.supported), "capabilities": {"tools": {}},
                      "serverInfo": dict(self.server_info)}
        elif method == "tools/list":
            result = {"tools": TOOLS, "ttlMs": 60000}
        elif method == "tools/call":
            result = self._call(params)
            if "error" in result:
                return _error(rid, -32602, result["error"])
        else:
            return _error(rid, -32601, f"Method not found: {method}")
        result.setdefault("resultType", "complete")
        result["_meta"] = {META_SERVER: dict(self.server_info)}
        return _result(rid, result)

    # -- the older generation --

    def _old(self, method, params, rid, is_request, session) -> dict | None:
        if method == "notifications/initialized":
            self.initialized.add(session)
            return None
        if not is_request:
            return None
        if method == "initialize":
            return _result(rid, {"protocolVersion": self.old_version, "capabilities": {"tools": {}},
                                 "serverInfo": dict(self.server_info)})
        if method == "ping":
            return _result(rid, {})
        if method in ("tools/list", "tools/call") and session not in self.initialized:
            return _error(rid, -32600, "not initialized")
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "tools/call":
            result = self._call(params)
            if "error" in result:
                return _error(rid, -32602, result["error"])
            return _result(rid, result)
        return _error(rid, -32601, f"Method not found: {method}")

    # -- tools --

    def _call(self, params) -> dict:
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if self.crash_on and name == self.crash_on:
            sys.stderr.write(f"fake-mcp: fatal error while running {name}: simulated crash\n")
            sys.stderr.flush()
            os._exit(3)
        if name == "echo":
            return {"content": [_text(args.get("text", ""))]}
        if name == "add":
            total = args.get("a", 0) + args.get("b", 0)
            return {"content": [_text(total)], "structuredContent": {"sum": total}}
        if name == "fail":
            return {"content": [_text(args.get("text", "failed"))], "isError": True}
        if name == "image":
            return {"content": [{"type": "image", "data": PNG_1X1, "mimeType": "image/png"}]}
        if name == "slow":
            seconds = float(args.get("seconds", 1))
            if self.stopping.wait(seconds):
                return {"content": [_text(f"stopped before {seconds:g} s")]}
            return {"content": [_text(f"waited {seconds:g} s")]}
        if name == "input_required":
            return {"resultType": "input_required",
                    "inputRequests": {"confirm": {"method": "elicitation/create",
                                                  "params": {"message": "Are you sure?",
                                                             "requestedSchema": _obj({"ok": {"type": "boolean"}})}}}}
        return {"error": f"Unknown tool: {name}"}


# ---------------- Streamable HTTP ----------------

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeMCPHttp:
    def __init__(self, port: int = 0, old: bool = False, sse: bool = False, auth_base: str | None = None):
        self.core = FakeMCPCore(old=old)
        self.old = old
        self.sse = sse
        self.auth_base = auth_base.rstrip("/") if auth_base else None
        self.calls = self.core.calls
        self.requests: list[dict] = []
        self.sessions: set[str] = set()
        self.break_streams = 0       # the next n SSE replies end before the reply
        self.challenge: str | None = None       # replaces the WWW-Authenticate value on a 401
        self.discover_status: int | None = None  # answer server/discover with this HTTP status
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes = b"", ctype="application/json", extra: dict | None = None):
                outer.requests.append({"method": self.command, "path": self.path, "status": code,
                                       "headers": Headers(self.headers.items())})
                self.send_response(code)
                if body or code != 202:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code, obj, extra=None):
                self._send(code, json.dumps(obj).encode(), "application/json", extra)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/.well-known/oauth-protected-resource") and outer.auth_base:
                    return self._json(200, {"resource": outer.mcp_url, "authorization_servers": [outer.auth_base],
                                            "bearer_methods_supported": ["header"]})
                if path.startswith("/.well-known/"):
                    return self._json(404, {"error": "not found"})
                return self._json(405, {"error": "this server does not open a stream on GET"})

            def do_DELETE(self):
                sid = self.headers.get("Mcp-Session-Id")
                if outer.old and sid in outer.sessions:
                    outer.sessions.discard(sid)
                    return self._json(200, {})
                return self._json(405, {"error": "nothing to delete"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if urllib.parse.urlsplit(self.path).path.startswith("/.well-known/"):
                    return self._json(405, {"error": "method not allowed"})
                if outer.auth_base and not outer._token_ok(self.headers.get("Authorization")):
                    value = outer.challenge or \
                        f'Bearer resource_metadata="{outer.url}/.well-known/oauth-protected-resource"'
                    return self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": value})
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    return self._json(400, _error(None, -32700, "Parse error"))
                if not isinstance(msg, dict):
                    return self._json(400, _error(None, -32600, "Batches are not supported"))
                headers = Headers(self.headers.items())
                method = msg.get("method")
                if method == "server/discover" and outer.discover_status:
                    return self._json(outer.discover_status, _error(msg.get("id"), -32601, "Not found"))
                extra = {}
                session = None
                if outer.old:
                    if method not in ("initialize", "server/discover"):
                        session = headers.get("Mcp-Session-Id")
                        if not session:
                            return self._json(400, _error(msg.get("id"), -32600, "Missing Mcp-Session-Id"))
                        if session not in outer.sessions:
                            return self._json(404, _error(msg.get("id"), -32600, "Session not found"))
                    reply = outer.core.handle(msg, headers, session)
                    if method == "initialize" and reply and "result" in reply:
                        sid = uuid.uuid4().hex
                        outer.sessions.add(sid)
                        extra["Mcp-Session-Id"] = sid
                else:
                    reply = outer.core.handle(msg, headers, None)
                if reply is None:
                    return self._send(202, b"", extra=extra)
                err = reply.get("error") or {}
                if not outer.old and err.get("code") in (-32600, -32022):
                    return self._json(400, reply, extra)   # header, _meta and version problems are bad requests
                if outer.sse:
                    return self._sse(reply, extra)
                return self._json(200, reply, extra)

            def _sse(self, reply, extra):
                with outer._lock:
                    broken = outer.break_streams > 0
                    if broken:
                        outer.break_streams -= 1
                outer.requests.append({"method": "POST", "path": self.path, "status": 200, "sse": True,
                                       "broken": broken, "headers": Headers(self.headers.items())})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    if broken:   # the stream ends before the reply (HTTP/1.0: closing the connection ends it)
                        self.wfile.write(b": keep-alive\n\nevent: message\n")
                    else:
                        self.wfile.write(b"event: message\ndata: " + json.dumps(reply).encode() + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.mcp_url = self.url + "/mcp"
        self._thread: threading.Thread | None = None

    # The protocol knobs live on the core; these let tests set them on the server.
    @property
    def supported(self):
        return self.core.supported

    @supported.setter
    def supported(self, value):
        self.core.supported = list(value)

    @property
    def old_version(self):
        return self.core.old_version

    @old_version.setter
    def old_version(self, value):
        self.core.old_version = value

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def _token_ok(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        url = f"{self.auth_base}/introspect?" + urllib.parse.urlencode({"token": token})
        try:
            with _direct.open(url, timeout=10) as r:
                return bool(json.loads(r.read().decode()).get("active"))
        except Exception:
            return False

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.core.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(5)


# ---------------- stdio ----------------

def run_stdio(old: bool) -> None:
    core = FakeMCPCore(old=old)
    core.crash_on = os.environ.get("FAKE_MCP_CRASH_ON") or None
    out = sys.stdout.buffer
    write_lock = threading.Lock()

    def send(obj) -> None:
        data = json.dumps(obj).encode("utf-8") + b"\n"
        with write_lock:
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                pass

    def work(msg) -> None:
        if msg.get("method") == "tools/call":   # a log notification first; the client must skip it
            send({"jsonrpc": "2.0", "method": "notifications/message",
                  "params": {"level": "info", "data": f"calling {(msg.get('params') or {}).get('name')}"}})
        reply = core.handle(msg, None, "stdio")
        if reply is not None:
            send(reply)

    sys.stderr.write(f"fake-mcp: ready ({'old' if old else 'new'} protocol)\n")
    sys.stderr.flush()
    workers: list[threading.Thread] = []
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            send(_error(None, -32700, "Parse error"))
            continue
        if isinstance(msg, dict) and "id" in msg and "method" in msg:
            t = threading.Thread(target=work, args=(msg,), daemon=True)   # requests run side by side
            t.start()
            workers.append(t)
        elif isinstance(msg, dict):
            core.handle(msg, None, "stdio")   # notifications in order, before any later request
    core.stopping.set()   # input ended: wake slow() and let replies in flight go out before exiting
    for t in workers:
        t.join(2)


def mcp_main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="A fake MCP server for tests")
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--http", type=int, metavar="PORT")
    ap.add_argument("--old", action="store_true")
    ap.add_argument("--sse", action="store_true")
    ap.add_argument("--auth", metavar="URL")
    a = ap.parse_args(argv)
    if a.stdio:
        run_stdio(a.old)
        return
    if a.http is None:
        ap.error("use --stdio or --http PORT")
    server = FakeMCPHttp(port=a.http, old=a.old, sse=a.sse, auth_base=a.auth).start()
    print(f"Fake MCP server at {server.mcp_url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()




# ======================================================================== Bench wiring

BLOCKED_REASONS = ("ddg_html: blocked by a bot challenge (HTTP 202, the page asks to confirm this is not a bot); "
                   "brave: not set up; searxng: not set up")
BENCH_MCP = "benchmcp"     # the connector key of the fake MCP server while a scenario uses it
DOWN_DETAIL = "The last health check failed: the service answered HTTP 401 (the API key was refused)."
NOT_FOUND = b"<!doctype html><html><head><title>Not found</title></head><body><h1>Not found</h1></body></html>"


def stripe_test_key() -> str:
    """A Stripe test key the fake accepts, built at run time so secret scanners do not flag the source."""
    return "sk" + "_test_" + "bench" + secrets.token_hex(8)


class Mocks:
    """What install() put in place for one scenario, and what it saw. remove() undoes everything (safe to call twice).

    Records for the runner: replies (one per model reply: kind, seconds, tokens, eval_count, eval_seconds, calls),
    calls (one per executed tool call: name and args after aliases, the name the model used, result, step, run_id),
    phone (what would have reached the owner's phone), searches and fetches (what the agent looked up)."""

    def __init__(self, spec: dict | None, company: dict | None, settings: dict | None, deadline: float | None):
        self.spec = dict(spec or {})
        self.company = company
        self.company_id = (company or {}).get("id")
        self.thread = threading.get_ident()
        self.settings = dict(settings or {})   # overrides of the owner's settings, in the bench thread only
        self.deadline = deadline               # time.monotonic() after which the run is stopped (timeout_s)
        self.kill_after: int | None = None     # the kill switch comes on once this many tool calls ran
        self.as_active = False                 # the bench company reads as active (see _patch_company)
        self.kind = ""                         # the kind of run going on: founding, cycle, task, chat or job
        self.replies: list[dict] = []
        self.calls: list[dict] = []
        self.phone: list[dict] = []
        self.searches: list[str] = []
        self.fetches: list[str] = []
        self.stripe: FakeStripe | None = None
        self.telegram: FakeTelegram | None = None
        self.mcp: FakeMCPHttp | None = None
        self.timed_out = False
        self.active = True
        self._undo: list = []
        self._servers: list = []
        self._depth = threading.local()

    def mine(self) -> bool:
        """True in the thread that installed the mocks, while they are installed."""
        return self.active and threading.get_ident() == self.thread

    def stop_now(self) -> bool:
        """The bench thread's kill switch: the scenario's time is up, or its scripted kill switch came on."""
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.timed_out = True
            return True
        return self.kill_after is not None and len(self.calls) >= self.kill_after

    def patch(self, obj, name: str, fn) -> object:
        old = getattr(obj, name)
        setattr(obj, name, fn)
        self._undo.append(lambda o=obj, n=name, v=old: setattr(o, n, v))
        return old

    def on_remove(self, fn) -> None:
        self._undo.append(fn)

    def server(self, srv):
        self._servers.append(srv)
        return srv.start()

    def remove(self) -> None:
        if not self.active:
            return
        self.active = False
        while self._undo:
            fn = self._undo.pop()
            try:
                fn()
            except Exception:   # one failed restore must not stop the others
                traceback.print_exc()
        for srv in reversed(self._servers):
            try:
                srv.stop()
            except Exception:
                traceback.print_exc()
        self._servers.clear()


def install(spec: dict | None, company: dict | None = None, settings: dict | None = None,
            deadline: float | None = None) -> Mocks:
    """Wire one scenario's mocks (see the module docstring) into this process for the calling thread."""
    m = Mocks(spec, company, settings, deadline)
    try:
        _patch_settings(m)
        _patch_company(m)
        _patch_web(m)
        _patch_phone(m)
        _patch_connectors(m)
        _patch_browser(m)
        _patch_tools(m)
        _record(m)
        _services(m)
    except Exception:
        m.remove()
        raise
    return m


# ---------------------------------------------------------------- settings and the company

def _patch_settings(m: Mocks) -> None:
    from .. import db   # imported here: the stdio fake MCP server loads this module without Holdco
    orig = db.get_settings

    def get_settings() -> dict:
        s = orig()
        if not m.mine():
            return s
        s.update(copy.deepcopy(m.settings))
        if m.stop_now():
            s["kill_switch"] = True   # agents.run stops at the next step boundary
        return s
    m.patch(db, "get_settings", get_settings)


def _patch_company(m: Mocks) -> None:
    """agents.run refuses task and cycle runs for a company that is not active; a bench company has status 'bench'
    so the scheduler never dispatches it. While the runner sets as_active, the bench thread reads it as active.
    (Not needed once agents.run accepts status 'bench'; founding runs never set it, so a finished stage does not start
    the next one in the background.)"""
    from .. import db
    orig = db.get_company

    def get_company(cid):
        row = orig(cid)
        if row and m.as_active and m.mine() and row.get("id") == m.company_id and row.get("status") == "bench":
            row = {**row, "status": "active"}
        return row
    m.patch(db, "get_company", get_company)


# ---------------------------------------------------------------- search and web pages

def _scripted_page(pages: dict, url: str) -> tuple[int, str, bytes, str]:
    """web.http_get's answer for a URL: the scripted page, its status, or a 404."""
    def norm(u: str) -> str:
        return str(u or "").split("#", 1)[0].rstrip("/")
    page = pages.get(url)
    if page is None:
        page = next((v for k, v in pages.items() if norm(k) == norm(url)), None)
    if page is None:
        return 404, "text/html; charset=utf-8", NOT_FOUND, url
    if isinstance(page, int):
        return page, "text/html; charset=utf-8", (NOT_FOUND if page == 404 else
                                                  f"<html><title>HTTP {page}</title></html>".encode()), url
    text = str(page)
    is_html = text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
    return 200, "text/html; charset=utf-8" if is_html else "text/plain; charset=utf-8", text.encode("utf-8"), url


def _patch_web(m: Mocks) -> None:
    from .. import doctor, web
    orig_search, orig_get, orig_check, orig_status = web.search, web.http_get, web.check_public_url, \
        doctor.search_status
    blocked = m.spec.get("search") == "blocked"

    def search(query, n=6, settings=None, ignore_cooldown=False):
        if not m.mine():
            return orig_search(query, n, settings, ignore_cooldown)
        m.searches.append(str(query))
        if blocked:
            raise web.WebError("All search providers failed: " + BLOCKED_REASONS)
        rows = [dict(r) for r in (m.spec.get("search") or []) if isinstance(r, dict) and r.get("url")]
        return rows[:max(1, min(int(n or 6), 10))]

    def http_get(url, headers=None, timeout=25):
        if not m.mine():
            return orig_get(url, headers, timeout)
        m.fetches.append(str(url))
        return _scripted_page(m.spec.get("fetch") or {}, str(url))

    def check_public_url(url):
        if not m.mine():
            return orig_check(url)
        if not re.match(r"^https?://[^/\s]+", str(url or ""), re.I):   # no DNS lookup in a bench: nothing is real
            raise web.WebError("Only public http(s) URLs can be fetched.")
        return url

    def search_status():
        if not m.mine():
            return orig_status()
        if blocked:
            return {"key": "search", "label": "Web search", "status": "bad",
                    "detail": "every provider failed: " + BLOCKED_REASONS}
        return {"key": "search", "label": "Web search", "status": "ok", "detail": "scripted results (Holdco Bench)"}

    m.patch(web, "search", search)
    m.patch(web, "http_get", http_get)
    m.patch(web, "check_public_url", check_public_url)
    m.patch(doctor, "search_status", search_status)
    cid = m.company_id
    m.on_remove(lambda: web.LAST_SEARCH_ALERT.pop(cid, None))   # the "search is down" note of the bench company


# ---------------------------------------------------------------- the owner's phone

def _to_fake_telegram(m: Mocks, text: str, buttons=None) -> None:
    tg = m.telegram
    if not tg or not text:
        return
    body = {"chat_id": tg.chat_id, "text": text[:4000], "parse_mode": "HTML"}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    req = urllib.request.Request(f"{tg.url}/bot{tg.token}/sendMessage", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _direct.open(req, timeout=10) as r:
            r.read()
    except Exception as e:   # the fake phone is a record, never a reason for a scenario to fail
        print(f"Holdco Bench: fake Telegram send failed: {e}")


def _patch_phone(m: Mocks) -> None:
    """Nothing a scenario does reaches the owner's phone: messages are recorded (and sent to the fake Telegram when
    the scenario starts one). A request of a real company announced from the bench thread (watchdog.tick looks at
    every active company) still goes to the phone."""
    from .. import notify

    def keep(via: str, text: str, buttons=None, kind: str = "") -> None:
        m.phone.append({"via": via, "text": str(text or ""), "kind": kind, "buttons": buttons or []})
        _to_fake_telegram(m, str(text or ""), buttons)

    orig_send, orig_later, orig_requested, orig_error = notify.send, notify.send_later, \
        notify.approval_requested, notify.error

    def send(text, buttons=None, kind="info", *, dedupe=True):
        if not m.mine():
            return orig_send(text, buttons, kind, dedupe=dedupe)
        keep("send", text, buttons, kind)
        return True

    def send_later(text, buttons=None, kind="info", *, dedupe=True):
        if not m.mine():
            return orig_later(text, buttons, kind, dedupe=dedupe)
        keep("send_later", text, buttons, kind)

    def approval_requested(approval):
        if not m.mine() or (approval or {}).get("company_id") != m.company_id:
            return orig_requested(approval)
        try:
            text, buttons, kind = notify.build_approval(approval)
        except Exception as e:
            text, buttons, kind = f"Request #{(approval or {}).get('id')} ({e})", None, "request"
        keep("approval_requested", text, buttons, kind)

    def error(company, message):
        if not m.mine() or (company and company.get("id") != m.company_id):
            return orig_error(company, message)
        keep("error", message, None, "error")

    m.patch(notify, "send", send)
    m.patch(notify, "send_later", send_later)
    m.patch(notify, "approval_requested", approval_requested)
    m.patch(notify, "error", error)


# ---------------------------------------------------------------- connectors and services

def _patch_connectors(m: Mocks) -> None:
    """The bench company sees only the connectors its scenario sets up: the owner's global ones (a Playwright
    browser, a Brave key) would make a scenario behave differently on every PC."""
    from .. import connectors
    orig_visible, orig_instance = connectors.visible, connectors.instance

    def visible(company):
        rows = orig_visible(company)
        if not m.mine():
            return rows
        return [r for r in rows if r.get("company_id") == m.company_id]

    def instance(key, company):
        inst = orig_instance(key, company)
        if not m.mine():
            return inst
        return inst if inst and inst.get("company_id") == m.company_id else None

    m.patch(connectors, "visible", visible)
    m.patch(connectors, "instance", instance)


def _add_instance(m: Mocks, key: str, config: dict, status: str, detail: str) -> int:
    """A connectors row of the bench company, removed with its vault secrets when the mocks go."""
    from .. import connectors, db
    ts = db.now()
    iid = db.run("INSERT INTO connectors(company_id, key, enabled, config, status, detail, last_check, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (m.company_id, key, 1, json.dumps(config), status, detail, ts, ts))

    def drop() -> None:
        try:
            connectors.remove(iid)   # closes an MCP client and deletes the instance's vault secrets
        finally:
            db.run("DELETE FROM connectors WHERE id=?", (iid,))
    m.on_remove(drop)
    return iid


def _services(m: Mocks) -> None:
    from .. import connectors, vault
    spec = m.spec
    if spec.get("telegram"):
        m.telegram = m.server(FakeTelegram())
    if spec.get("stripe") and m.company_id:
        m.stripe = m.server(FakeStripe())
        iid = _add_instance(m, "stripe", {"api_base": m.stripe.url}, "ok", "Fake Stripe (Holdco Bench), test mode.")
        vault.put(f"connector.{iid}.api_key", stripe_test_key())
    if spec.get("mcp") and m.company_id:
        m.mcp = m.server(FakeMCPHttp())
        connectors.DEFINITIONS[BENCH_MCP] = {
            "label": "Bench tools (MCP)", "category": "Test", "kind": "mcp-http", "scope": "company", "oauth": False,
            "setup": {"where_to_get_key": "Nothing: Holdco Bench adds this connector while a scenario runs.",
                      "permissions": [], "fields": [], "test_mode": "Test only.", "docs_url": "",
                      "agents_can": ["Echo text and add numbers"]},
            "mcp": {"url": m.mcp.mcp_url, "headers": {},
                    "classify": [("echo", "read"), ("add", "read"), ("image", "read"), ("fail", "read")],
                    "allow": {"*": ["*"]}, "blocked": ["slow", "input_required"]},
        }
        m.on_remove(lambda: connectors.DEFINITIONS.pop(BENCH_MCP, None))
        _add_instance(m, BENCH_MCP, {}, "ok", "Fake MCP server (Holdco Bench).")
    down = spec.get("connector_down")
    if down and m.company_id:
        _add_instance(m, str(down), {}, "bad", DOWN_DETAIL)


# ---------------------------------------------------------------- browser_check

def _patch_browser(m: Mocks) -> None:
    """Playwright "absent": the static check, which reads the page from disk (the dashboard's preview may belong to
    another data folder, or to nothing). A scripted output replaces the whole check."""
    from .. import browser, tools
    scripted = m.spec.get("playwright", "absent")
    if scripted not in (None, "", "absent"):
        t = tools.REGISTRY["browser_check"]
        orig_fn = t.fn

        def browser_check(ctx, path_or_url, mobile=True):
            if not m.mine():
                return orig_fn(ctx, path_or_url, mobile)
            if isinstance(scripted, dict):
                return str(scripted.get(path_or_url) or scripted.get("*") or "Static check only: no scripted page")
            return str(scripted)
        m.patch(t, "fn", browser_check)
        return
    orig_fetch, orig_pw = browser._fetch, browser._playwright

    def _fetch(url):
        if not m.mine():
            return orig_fetch(url)
        return 0, "", "Holdco Bench reads the page from disk"

    def _playwright(company):
        if not m.mine():
            return orig_pw(company)
        return None, "Playwright is not set up"

    m.patch(browser, "_fetch", _fetch)
    m.patch(browser, "_playwright", _playwright)


# ---------------------------------------------------------------- scripted tool failures

def _patch_tools(m: Mocks) -> None:
    from .. import tools
    if not m.spec.get("write_file_fail_once"):
        return
    t = tools.REGISTRY["write_file"]
    orig_fn, state = t.fn, {"failed": False}

    def write_file(ctx, *args, **kwargs):
        if m.mine() and not state["failed"]:
            state["failed"] = True
            raise tools.ToolError("the disk was busy and the file was not saved. Try again.")
        return orig_fn(ctx, *args, **kwargs)
    m.patch(t, "fn", write_file)


# ---------------------------------------------------------------- what the model and the tools did

def _record(m: Mocks) -> None:
    """Record every model reply (for tool-call validity and tokens per second) and every tool call with its result
    (for the assertions), in the bench thread."""
    from .. import llm
    from .. import tools as tool_mod
    orig_chat, orig_execute = llm.chat, tool_mod.execute

    def chat(messages, tools=None, model=None, settings=None, *args, **kwargs):
        depth = getattr(m._depth, "n", 0)
        if not m.mine() or depth:   # llm.chat calls itself on a fallback: count the outer reply only
            return orig_chat(messages, tools, model, settings, *args, **kwargs)
        m._depth.n = depth + 1
        t0 = time.monotonic()
        try:
            reply = orig_chat(messages, tools, model, settings, *args, **kwargs)
        finally:
            m._depth.n = depth
        m.replies.append({"kind": m.kind, "seconds": time.monotonic() - t0, "tokens": int(reply.tokens or 0),
                          "eval_count": int(getattr(reply, "eval_count", 0) or 0),
                          "eval_seconds": float(getattr(reply, "eval_seconds", 0.0) or 0.0),
                          "calls": len(reply.tool_calls or []), "mode": reply.mode})
        return reply

    def execute(ctx, name, args):
        if not m.mine():
            return orig_execute(ctx, name, args)
        if name in (getattr(ctx, "loaded_by_name", None) or {}):
            rname, rargs = name, dict(args or {})
        else:
            rname, rargs = tool_mod.resolve(ctx, name, args)
        out = orig_execute(ctx, name, args)
        m.calls.append({"name": rname, "asked": name, "args": rargs, "result": str(out)[:6000],
                        "step": len(m.replies), "run_id": getattr(ctx, "run_id", 0), "kind": m.kind})
        return out

    m.patch(llm, "chat", chat)
    m.patch(tool_mod, "execute", execute)
