"""A fake Plausible Stats API v2 and a fake Umami API on one server, so the analytics connectors run in tests
without network or real accounts.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point a plausible or umami card's base_url at `.url`.

    fake = FakePlausible().start()
    ... save the cards with base_url fake.url, poll the metrics ...
    fake.queries[-1]["body"], fake.requests[-1]["headers"]
    fake.stop()
    python tests/fake_plausible.py 12003   # standalone, prints the URL

Plausible: POST /api/v2/query with `Authorization: Bearer <plausible_key>` (anything else: 401 {"error"}) and a JSON
query {site_id, metrics, date_range, dimensions, order_by, pagination}. site_id must be `site` (else 400); metrics
must be among visitors, visits, pageviews, events (else 400); date_range must be a period name or two ISO dates
(else 400). No dimension: one row of `stats`; event:goal: one row per entry of `goals` with [events]; event:page:
`pages` rows ([visitors, pageviews]); visit:source: `sources` rows ([visitors]). Every query is kept in `queries`.

Umami: under /api (self-hosted) and /v1 (cloud). POST <root>/auth/login {username, password} answers {"token"} for
`umami_user`, else 401. GET <root>/websites/<website>/stats?startAt=&endAt= (milliseconds, both required, else 400)
answers `stats` in `umami_shape` "value" ({"visitors": {"value": n, "prev": 0}, ...}) or "flat" ({"visitors": n,
...}); GET <root>/websites/<website>/metrics?type=&startAt=&endAt=[&limit=] answers [{"x", "y"}] for the types in
`umami_page_types` (pages), referrer (`sources`) and event (`umami_events`), other types 400. Auth: a token issued
by the login, or `umami_token` as a Bearer token or in x-umami-api-key; else 401. An unknown website id is 404.
`expire_logins()` forgets the issued tokens (the next call with one gets 401).

Records: requests ([{"method", "path", "query", "body", "headers"}]), queries (the Plausible query bodies).
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PERIODS = ("day", "7d", "28d", "30d", "91d", "month", "6mo", "12mo", "year", "all")
METRICS = ("visitors", "visits", "pageviews", "events")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class FakePlausible:
    def __init__(self, port: int = 0, plausible_key: str = "plausible-test-key", site: str = "pawplan.example",
                 umami_token: str = "umami-test-token", umami_user=("viewer", "umami-test-pass"),
                 website: str = "web-1"):
        self.plausible_key, self.site = plausible_key, site
        self.umami_token, self.umami_user, self.website = umami_token, tuple(umami_user), website
        self.stats = {"visitors": 120, "visits": 150, "pageviews": 410, "events": 450}
        self.goals = [("Signup", 7), ("Purchase", 2)]
        self.pages = [("/", 80, 200), ("/pricing", 30, 60), ("/blog/dog-food", 10, 12)]
        self.sources = [("Google", 50), ("Direct / None", 40), ("news.ycombinator.com", 12)]
        self.umami_events = [("signup", 5), ("checkout", 1)]
        self.umami_shape = "value"
        self.umami_page_types = {"url"}
        self.requests: list[dict] = []
        self.queries: list[dict] = []
        self.logins: set[str] = set()
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code: int, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _record(self, body):
                parts = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
                headers = {k.lower(): v for k, v in self.headers.items()}
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": parts.path, "query": query,
                                           "body": body, "headers": headers})
                return parts.path, query

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return None

            def do_GET(self):
                path, query = self._record(None)
                m = re.match(r"^/(?:api|v1)/websites/([^/]+)/(stats|metrics)$", path)
                if not m:
                    return self._json(404, {"error": "Not found"})
                if not outer._umami_ok(self.headers):
                    return self._json(401, {"error": "Unauthorized"})
                if m.group(1) != outer.website:
                    return self._json(404, {"error": "Website not found"})
                if not (query.get("startAt", "").isdigit() and query.get("endAt", "").isdigit()):
                    return self._json(400, {"error": "startAt and endAt are required"})
                if m.group(2) == "stats":
                    return self._json(200, outer._umami_stats())
                kind = query.get("type", "")
                if kind in outer.umami_page_types:
                    rows = [(p, views) for p, _, views in outer.pages]
                elif kind == "referrer":
                    rows = [(s, v) for s, v in outer.sources if s != "Direct / None"]
                elif kind == "event":
                    rows = list(outer.umami_events)
                else:
                    return self._json(400, {"error": f"Invalid type: {kind}"})
                limit = int(query["limit"]) if query.get("limit", "").isdigit() else len(rows)
                return self._json(200, [{"x": x, "y": y} for x, y in rows[:limit]])

            def do_POST(self):
                body = self._body()
                path, _ = self._record(body)
                if path == "/api/v2/query":
                    return self._json(*outer._plausible(self.headers, body))
                if re.match(r"^/(?:api|v1)/auth/login$", path):
                    b = body if isinstance(body, dict) else {}
                    if (b.get("username"), b.get("password")) != outer.umami_user:
                        return self._json(401, {"error": "Incorrect username and/or password."})
                    token = "umami-login-" + secrets.token_hex(8)
                    with outer._lock:
                        outer.logins.add(token)
                    return self._json(200, {"token": token, "user": {"id": "u-1", "username": b["username"]}})
                return self._json(404, {"error": "Not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    # -- Plausible --

    def _plausible(self, headers, body) -> tuple[int, dict]:
        if (headers.get("Authorization") or "") != f"Bearer {self.plausible_key}":
            return 401, {"error": "Invalid API key or site ID. Please make sure you're using a valid API key with "
                                  "access to the site you've requested."}
        if not isinstance(body, dict):
            return 400, {"error": "Invalid JSON"}
        with self._lock:
            self.queries.append(body)
        if body.get("site_id") != self.site:
            return 400, {"error": f"Site not found: {body.get('site_id')}"}
        metrics = body.get("metrics")
        if not isinstance(metrics, list) or not metrics or any(m not in METRICS for m in metrics):
            return 400, {"error": f"Invalid metrics: {metrics}"}
        dr = body.get("date_range")
        if not (dr in PERIODS or (isinstance(dr, list) and len(dr) == 2 and all(_DATE.match(str(d)) for d in dr))):
            return 400, {"error": f"Invalid date_range: {dr}"}
        dims = body.get("dimensions") or []
        if not dims:
            rows = [{"metrics": [self.stats[m] for m in metrics], "dimensions": []}]
        elif dims == ["event:goal"]:
            rows = [{"metrics": [n for _ in metrics], "dimensions": [g]} for g, n in self.goals]
        elif dims == ["event:page"]:
            rows = [{"metrics": [v if m == "visitors" else pv for m in metrics], "dimensions": [p]}
                    for p, v, pv in self.pages]
        elif dims == ["visit:source"]:
            rows = [{"metrics": [v for _ in metrics], "dimensions": [s]} for s, v in self.sources]
        else:
            return 400, {"error": f"Invalid dimensions: {dims}"}
        limit = (body.get("pagination") or {}).get("limit")
        if isinstance(limit, int):
            rows = rows[:limit]
        return 200, {"results": rows, "meta": {}, "query": body}

    # -- Umami --

    def _umami_ok(self, headers) -> bool:
        auth = headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        with self._lock:
            if token and token in self.logins:
                return True
        return self.umami_token in (token, headers.get("x-umami-api-key") or "")

    def _umami_stats(self) -> dict:
        names = {"pageviews": "pageviews", "visitors": "visitors", "visits": "visits"}
        out = {k: self.stats[v] for k, v in names.items()}
        out.update(bounces=12, totaltime=3600)
        if self.umami_shape == "value":
            return {k: {"value": v, "prev": 0} for k, v in out.items()}
        return {**out, "comparison": {k: 0 for k in out}}

    def expire_logins(self) -> None:
        with self._lock:
            self.logins.clear()

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakePlausible(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Plausible and Umami at {srv.url} (Plausible key {srv.plausible_key}, site {srv.site}; Umami token "
          f"{srv.umami_token}, website {srv.website})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
