"""A fake of every outside service Holdco connects to (Stripe, Lemon Squeezy, Gumroad, Plausible, GoatCounter, Netlify,
X, Bluesky, Mastodon, LinkedIn, Cal.com, RDAP, Telegram, ntfy and webhooks), so connectors can be tested end to end
without accounts or network access.

    fake = FakeAPIServer().start()
    os.environ.update(fake.env())        # point every connector at the fake
    ...
    fake.calls                           # every request it received
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TODAY = _dt.date.today().isoformat()
NOW_TS = int(_dt.datetime.now(_dt.timezone.utc).timestamp())


class FakeAPIServer:
    def __init__(self, port: int = 0):
        self.calls: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj=None, headers=None, text=None):
                body = (text if text is not None else json.dumps(obj if obj is not None else {})).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = raw.decode("utf-8", "replace")
                parsed = urllib.parse.urlparse(self.path)
                call = {"method": method, "path": parsed.path, "query": urllib.parse.parse_qs(parsed.query),
                        "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                outer.calls.append(call)
                return call

            def do_GET(self):
                self._route(self._record("GET"))

            def do_POST(self):
                self._route(self._record("POST"))

            def _route(self, c):
                p, h, b, q = c["path"], c["headers"], c["body"], c["query"]
                auth = h.get("authorization", "")
                # ---- Stripe
                if p.startswith("/stripe/"):
                    if auth != "Bearer rk_test_ok":
                        return self._reply(401, {"error": {"message": "Invalid API Key provided", "type": "invalid_request_error"}})
                    if p == "/stripe/v1/balance":
                        return self._reply(200, {"available": [{"amount": 12345, "currency": "usd"}], "pending": [{"amount": 500, "currency": "usd"}]})
                    if p == "/stripe/v1/balance_transactions":
                        return self._reply(200, {"has_more": False, "data": [
                            {"id": "txn_1", "type": "charge", "amount": 2900, "fee": 114, "net": 2786, "currency": "usd", "created": NOW_TS - 86400,
                             "description": "Subscription creation"},
                            {"id": "txn_2", "type": "refund", "amount": -900, "fee": -56, "net": -844, "currency": "usd", "created": NOW_TS - 3600,
                             "description": "REFUND FOR CHARGE"},
                            {"id": "txn_3", "type": "payout", "amount": -5000, "fee": 0, "net": -5000, "currency": "usd", "created": NOW_TS - 7200,
                             "description": "STRIPE PAYOUT"},
                            {"id": "txn_4", "type": "stripe_fee", "amount": -200, "fee": 0, "net": -200, "currency": "usd", "created": NOW_TS - 100,
                             "description": "Billing fee"}]})
                    if p == "/stripe/v1/subscriptions":
                        return self._reply(200, {"has_more": False, "data": [
                            {"id": "sub_1", "status": "active", "items": {"data": [{"quantity": 1, "price": {"unit_amount": 2900, "recurring": {"interval": "month", "interval_count": 1}}}]}},
                            {"id": "sub_2", "status": "active", "items": {"data": [{"quantity": 2, "price": {"unit_amount": 24000, "recurring": {"interval": "year", "interval_count": 1}}}]}}]})
                # ---- Lemon Squeezy
                if p.startswith("/ls/"):
                    if auth != "Bearer ls_ok":
                        return self._reply(401, {"errors": [{"detail": "Unauthenticated."}]})
                    if p == "/ls/v1/users/me":
                        return self._reply(200, {"data": {"attributes": {"name": "Jack", "email": "jack@example.com"}}})
                    if p == "/ls/v1/orders":
                        return self._reply(200, {"links": {}, "data": [
                            {"id": "101", "attributes": {"total": 1900, "currency": "USD", "status": "paid", "created_at": f"{TODAY}T10:00:00Z",
                                                        "user_email": "a@b.co", "identifier": "abc-1", "first_order_item": {"product_name": "Guide"}, "refunded": False}},
                            {"id": "102", "attributes": {"total": 1900, "currency": "USD", "status": "refunded", "created_at": f"{TODAY}T11:00:00Z",
                                                        "user_email": "c@d.co", "identifier": "abc-2", "first_order_item": {"product_name": "Guide"},
                                                        "refunded": True, "refunded_amount": 1900, "refunded_at": f"{TODAY}T12:00:00Z"}}]})
                    if p == "/ls/v1/subscriptions":
                        return self._reply(200, {"links": {}, "data": [{"id": "7", "attributes": {"status": "active", "first_subscription_item": {"price_id": 55, "quantity": 1}}}]})
                    if p == "/ls/v1/prices/55":
                        return self._reply(200, {"data": {"attributes": {"unit_price": 1900, "renewal_interval_unit": "month", "renewal_interval_quantity": 1}}})
                # ---- Gumroad
                if p.startswith("/gumroad/"):
                    if q.get("access_token", [""])[0] != "gr_ok":
                        return self._reply(401, {"success": False, "message": "The access token is invalid."})
                    if p == "/gumroad/v2/user":
                        return self._reply(200, {"success": True, "user": {"name": "Jack"}})
                    if p == "/gumroad/v2/sales":
                        return self._reply(200, {"success": True, "sales": [
                            {"id": "s1", "email": "b@c.co", "created_at": f"{TODAY}T09:00:00Z", "price": 1500, "gumroad_fee": 150, "currency_symbol": "$",
                             "product_name": "Template", "refunded": False, "subscription_id": "subA", "recurrence": "monthly"}]})
                # ---- Plausible
                if p == "/plausible/api/v2/query":
                    if auth != "Bearer pl_ok":
                        return self._reply(401, {"error": "Invalid API key"})
                    if (b or {}).get("dimensions"):
                        return self._reply(200, {"results": [{"dimensions": ["/"], "metrics": [120]}, {"dimensions": ["/pricing"], "metrics": [40]}]})
                    return self._reply(200, {"results": [{"metrics": [340, 400, 900, 45.0, 120], "dimensions": []}]})
                # ---- GoatCounter
                if p.startswith("/goatcounter/"):
                    if auth != "Bearer gc_ok":
                        return self._reply(403, {"error": "no access"})
                    if p == "/goatcounter/api/v0/me":
                        return self._reply(200, {"user": {"email": "jack@example.com"}})
                    if p == "/goatcounter/api/v0/stats/total":
                        return self._reply(200, {"total": 210, "total_events": 300, "stats": []})
                    if p == "/goatcounter/api/v0/stats/hits":
                        return self._reply(200, {"hits": [{"path": "/", "count": 150}, {"path": "/blog", "count": 60}], "more": False})
                    if p == "/goatcounter/api/v0/stats/toprefs":
                        return self._reply(200, {"stats": [{"name": "news.ycombinator.com", "count": 80}], "more": False})
                # ---- Netlify
                if p.startswith("/netlify/"):
                    if auth != "Bearer nf_ok":
                        return self._reply(401, {"message": "Access Denied"})
                    if p == "/netlify/api/v1/user":
                        return self._reply(200, {"email": "jack@example.com", "full_name": "Jack"})
                    if p == "/netlify/api/v1/sites" and c["method"] == "POST":
                        return self._reply(201, {"id": "site_1", "ssl_url": "https://pawplan.netlify.app", "name": "pawplan"})
                    if p == "/netlify/api/v1/sites/site_1":
                        return self._reply(200, {"id": "site_1", "ssl_url": "https://pawplan.netlify.app", "name": "pawplan"})
                    if p == "/netlify/api/v1/sites/site_1/deploys":
                        return self._reply(200, {"id": "dep_1", "ssl_url": "https://pawplan.netlify.app"})
                    if p == "/netlify/api/v1/sites/site_1/submissions":
                        return self._reply(200, [
                            {"id": "sub1", "email": "jo@happypaws.example", "name": "Jo", "created_at": f"{TODAY}T08:00:00Z", "form_name": "waitlist",
                             "data": {"email": "jo@happypaws.example", "name": "Jo", "dog": "Rex", "ip": "1.2.3.4"}},
                            {"id": "sub2", "email": None, "name": None, "created_at": f"{TODAY}T07:00:00Z", "form_name": "contact",
                             "data": {"Email Address": "sam@example.com", "message": "Do you ship to Canada?"}}])
                # ---- Bluesky
                if p == "/bluesky/xrpc/com.atproto.server.createSession":
                    if (b or {}).get("password") != "app-pass":
                        return self._reply(401, {"error": "AuthenticationRequired", "message": "Invalid identifier or password"})
                    return self._reply(200, {"accessJwt": "jwt-1", "did": "did:plc:abc", "handle": "pawplan.bsky.social"})
                if p == "/bluesky/xrpc/com.atproto.repo.createRecord":
                    if auth != "Bearer jwt-1":
                        return self._reply(401, {"error": "AuthMissing"})
                    return self._reply(200, {"uri": "at://did:plc:abc/app.bsky.feed.post/3k2abc", "cid": "bafy"})
                # ---- Mastodon
                if p == "/mastodon/api/v1/accounts/verify_credentials":
                    return self._reply(200, {"username": "pawplan"}) if auth == "Bearer md_ok" else self._reply(401, {"error": "The access token is invalid"})
                if p == "/mastodon/api/v1/statuses":
                    return self._reply(200, {"url": "https://mastodon.example/@pawplan/1"}) if auth == "Bearer md_ok" else self._reply(401, {"error": "invalid"})
                # ---- X
                if p.startswith("/x/"):
                    if not auth.startswith("OAuth ") or "oauth_signature=" not in auth or 'oauth_consumer_key="xk"' not in auth:
                        return self._reply(401, {"title": "Unauthorized", "detail": "Unauthorized"})
                    if p == "/x/2/users/me":
                        return self._reply(200, {"data": {"id": "1", "username": "pawplan"}})
                    if p == "/x/2/tweets":
                        return self._reply(201, {"data": {"id": "1790", "text": (b or {}).get("text", "")}})
                # ---- LinkedIn
                if p == "/linkedin/v2/userinfo":
                    return self._reply(200, {"sub": "abc123", "name": "Jack"}) if auth == "Bearer li_ok" else self._reply(401, {"message": "Invalid access token"})
                if p == "/linkedin/rest/posts":
                    if auth != "Bearer li_ok" or not h.get("linkedin-version"):
                        return self._reply(401, {"message": "Invalid access token or missing version"})
                    return self._reply(201, {}, {"x-restli-id": "urn:li:share:999"}, text="")
                # ---- Cal.com
                if p.startswith("/calcom/"):
                    if auth != "Bearer cal_ok":
                        return self._reply(401, {"error": {"message": "Invalid API key"}})
                    if p == "/calcom/v2/me":
                        return self._reply(200, {"status": "success", "data": {"username": "jack", "email": "jack@example.com"}})
                    if p == "/calcom/v2/bookings":
                        return self._reply(200, {"status": "success", "data": [
                            {"uid": "b1", "title": "Intro call", "start": "2026-10-20T15:00:00Z", "end": "2026-10-20T15:30:00Z", "status": "accepted",
                             "attendees": [{"name": "Jo", "email": "jo@happypaws.example"}], "location": "https://meet.example/abc"}]})
                # ---- RDAP
                if p.startswith("/rdap/domain/"):
                    name = p.rsplit("/", 1)[-1]
                    if name == "pawplan.com":
                        return self._reply(200, {"events": [{"eventAction": "registration", "eventDate": "2010-01-02T00:00:00Z"},
                                                            {"eventAction": "expiration", "eventDate": "2027-01-02T00:00:00Z"}],
                                                 "status": ["client transfer prohibited"],
                                                 "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Namecheap"]]]}]})
                    if name == "pawplan.xyz":
                        return self._reply(404, {"errorCode": 404, "title": "Not Found"})
                    return self._reply(400, {"errorCode": 400})
                # ---- notifications
                if p.startswith("/telegram/bot"):
                    if "/bot123:abc/" not in p:
                        return self._reply(401, {"ok": False, "description": "Unauthorized"})
                    return self._reply(200, {"ok": True})
                if p.startswith("/ntfy/"):
                    return self._reply(200, {"id": "n1", "topic": p.rsplit("/", 1)[-1]})
                if p == "/hooks/slack":
                    return self._reply(200, text="ok")
                if p == "/hooks/discord":
                    return self._reply(204, text="")
                if p == "/hooks/generic":
                    return self._reply(200, {"ok": True, "url": "https://example.com/posted/1"})
                if p == "/hooks/fail":
                    return self._reply(500, {"error": "boom"})
                self._reply(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def env(self) -> dict:
        """Environment variables that point every connector at this server."""
        return {f"HOLDCO_API_BASE_{n.upper()}": f"{self.url}/{path}" for n, path in (
            ("stripe", "stripe"), ("lemonsqueezy", "ls"), ("gumroad", "gumroad"), ("netlify", "netlify"), ("x", "x"),
            ("linkedin", "linkedin"), ("rdap", "rdap"), ("telegram", "telegram"), ("ntfy", "ntfy"), ("plausible", "plausible"),
            ("bluesky", "bluesky"), ("calcom", "calcom"))}

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]
