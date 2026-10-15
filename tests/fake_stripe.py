"""A fake Stripe API, so the Stripe connector and the Phase 1 gate run without network or a real account.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`, and public lists of what it received. Point the connector's `api_base` at `.url`.

    stripe = FakeStripe().start()
    ... create payment links through the connector ...
    stripe.add_charge(amount=1900, currency="usd", paid=True)   # a test purchase
    stripe.add_subscription(unit_amount=12000, interval="year") # for MRR
    stripe.stop()
    python tests/fake_stripe.py 12001   # standalone, prints the URL

Auth: `Authorization: Bearer <key>`; a key containing "_test_" is accepted, any other key gets
401 {"error": {"message": "Invalid API Key"}}. Bodies are form-encoded; bracket keys are unflattened
(line_items[0][price] -> {"line_items": [{"price": ...}]}).

Endpoints: GET /v1/balance; POST /v1/products, GET /v1/products[/<id>]; POST /v1/prices, GET /v1/prices/<id>;
POST/GET /v1/payment_links, GET /v1/payment_links/<id>[/line_items]; GET /pay/<id> (an HTML checkout page with a
title, an h1 with the product name and a <form>, no auth); GET /v1/charges (created[gte], created[gt], limit,
starting_after); GET /v1/customers; GET /v1/subscriptions (status filter); POST /v1/refunds. Test hooks (no auth):
POST /_test/charges, /_test/customers, /_test/subscriptions with a JSON body, or the Python methods add_charge,
add_customer, add_subscription.

Records: requests ({"method", "path", "query", "body"} for every API call), plus the object stores products,
prices, links, charges, customers, subscriptions and refunds.
"""
from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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


if __name__ == "__main__":
    srv = FakeStripe(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Stripe at {srv.url} (keys containing _test_ are accepted)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
