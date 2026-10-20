"""A fake Netlify Forms API and a fake forms Worker (the contract of docs/forms-worker.js) on one server, so the
forms connector runs in tests without network or real accounts.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the forms card's hidden api_base at `.netlify_url` (Netlify mode) or its endpoint_url at
`.worker_url` (Worker mode).

    fake = FakeForms().start()
    fake.add_form("signup", fields=("name", "email", "consent"))
    fake.add_submission("signup", {"name": "Jo", "email": "jo@example.com", "consent": "on"})
    fake.add_worker_submission("signup", {"email": "sam@example.com"})
    fake.stop()
    python tests/fake_forms.py 12004   # standalone, prints the URLs

Netlify, under /api/v1, with `Authorization: Bearer <token>` (anything else: 401 {"code", "message"}):
GET /sites/<site>/forms -> [{id, site_id, name, paths, submission_count, fields: [{name, type}], created_at}] (an
unknown site is 404); GET /forms/<form id>/submissions?per_page=&page= -> that page of the form's submissions,
newest first: {id, number, email, name, first_name, last_name, company, summary, body, data: {...the fields, ip,
user_agent, referrer}, created_at, site_url, form_id, form_name}.

Worker, under /worker: GET with `X-Holdco-Secret: <secret>` (else 401) and form=<name>, after=<id>, since=<ISO
time>, limit=<1-1000> -> {"submissions": [{id, form, created_at, data}] oldest first, "total", "last_7_days", "more"}
(with form, only that form's submissions, listed and counted). POST
a urlencoded or JSON form stores it as the template does (form-name names the form, a filled-in bot-field is
dropped) and answers {"ok": true}. Ids are "<13-digit ms>-<8 hex>".

Records: requests ([{"method", "path", "query", "headers"}]).
"""
from __future__ import annotations

import datetime as _dt
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _iso(t: _dt.datetime, ms: bool = True) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond // 1000:03d}Z" if ms else "Z")


class FakeForms:
    def __init__(self, port: int = 0, token: str = "nf-test-token", site: str = "site-123",
                 secret: str = "worker-test-secret"):
        self.token, self.site, self.secret = token, site, secret
        self.forms: dict[str, dict] = {}            # form id -> form
        self.subs: dict[str, list[dict]] = {}       # form id -> submissions, oldest first
        self.worker_subs: list[dict] = []           # oldest first
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._seq = 0
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

            def _record(self):
                parts = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": parts.path, "query": query,
                                           "headers": {k.lower(): v for k, v in self.headers.items()}})
                return parts.path, query

            def do_GET(self):
                path, query = self._record()
                if path.rstrip("/") == "/worker":
                    if (self.headers.get("X-Holdco-Secret") or "") != outer.secret:
                        return self._json(401, {"error": "unauthorized"})
                    return self._json(200, outer._worker_list(query))
                if not path.startswith("/api/v1/"):
                    return self._json(404, {"code": 404, "message": "Not Found"})
                if (self.headers.get("Authorization") or "") != f"Bearer {outer.token}":
                    return self._json(401, {"code": 401, "message": "Access Denied: your token is invalid"})
                bits = path[len("/api/v1/"):].strip("/").split("/")
                if len(bits) == 3 and bits[0] == "sites" and bits[2] == "forms":
                    if bits[1] != outer.site:
                        return self._json(404, {"code": 404, "message": "Not Found"})
                    with outer._lock:
                        forms = [{**f, "submission_count": len(outer.subs.get(f["id"], []))}
                                 for f in outer.forms.values()]
                    return self._json(200, forms)
                if len(bits) == 3 and bits[0] == "forms" and bits[2] == "submissions":
                    with outer._lock:
                        if bits[1] not in outer.forms:
                            return self._json(404, {"code": 404, "message": "Not Found"})
                        newest = list(reversed(outer.subs.get(bits[1], [])))
                    per = max(1, min(int(query.get("per_page") or 100), 100))
                    page = max(1, int(query.get("page") or 1))
                    return self._json(200, newest[(page - 1) * per: page * per])
                return self._json(404, {"code": 404, "message": "Not Found"})

            def do_POST(self):
                path, _ = self._record()
                length = int(self.headers.get("Content-Length") or 0)
                raw = (self.rfile.read(length) if length else b"").decode("utf-8", "replace")
                if path.rstrip("/") != "/worker":
                    return self._json(405, {"error": "method not allowed"})
                if "json" in (self.headers.get("Content-Type") or ""):
                    try:
                        fields = {str(k): str(v) for k, v in (json.loads(raw or "{}") or {}).items()}
                    except (ValueError, AttributeError):
                        return self._json(400, {"error": "could not read the form"})
                else:
                    fields = {k: v for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True)}
                if not fields.get("bot-field", "").strip():
                    data = {k: v for k, v in fields.items() if k not in ("form-name", "bot-field")}
                    outer.add_worker_submission(fields.get("form-name") or "form", data)
                return self._json(200, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.netlify_url = self.url + "/api/v1"
        self.worker_url = self.url + "/worker"

    # -- Netlify --

    def add_form(self, name: str, fields=("name", "email", "consent")) -> dict:
        fid = secrets.token_hex(12)
        form = {"id": fid, "site_id": self.site, "name": name, "paths": ["/"],
                "fields": [{"name": f, "type": "email" if f == "email" else "text"} for f in fields],
                "created_at": _iso(_dt.datetime.now(_dt.timezone.utc))}
        with self._lock:
            self.forms[fid] = form
            self.subs[fid] = []
        return form

    def add_submission(self, form_name: str, data: dict, days_ago: float = 0) -> str:
        """A submission to a Netlify form (newest last; created days_ago in the past). Returns its id."""
        with self._lock:
            form = next(f for f in self.forms.values() if f["name"] == form_name)
            subs = self.subs[form["id"]]
            created = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
            sid = secrets.token_hex(12)
            name = str(data.get("name") or "")
            first, _, last = name.partition(" ")
            subs.append({"id": sid, "number": len(subs) + 1, "email": data.get("email"), "name": name or None,
                         "first_name": first or None, "last_name": last or None, "company": data.get("company"),
                         "summary": f"<strong>{name}</strong>", "body": None,
                         "data": {**data, "ip": "203.0.113.9", "user_agent": "Mozilla/5.0", "referrer": "/"},
                         "created_at": _iso(created), "site_url": "https://pawplan.example",
                         "form_id": form["id"], "form_name": form["name"]})
            subs.sort(key=lambda r: r["created_at"])
            return sid

    # -- the Worker --

    def add_worker_submission(self, form: str, data: dict, days_ago: float = 0) -> str:
        created = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
        with self._lock:
            self._seq += 1
            ms = int(created.timestamp() * 1000)
            sid = f"{ms:013d}-{self._seq:08x}"
            self.worker_subs.append({"id": sid, "form": form, "created_at": _iso(created), "data": dict(data)})
            self.worker_subs.sort(key=lambda s: s["id"])
            return sid

    def _worker_list(self, query: dict) -> dict:
        after = query.get("after") or ""
        since = 0
        if query.get("since"):
            t = _dt.datetime.strptime(query["since"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
            since = int(t.timestamp() * 1000)
        limit = max(1, min(int(query.get("limit") or 100), 1000))
        week_ago = int((time.time() - 7 * 86400) * 1000)
        form = (query.get("form") or "").lower()
        with self._lock:
            rows = [s for s in self.worker_subs if not form or s["form"].lower() == form]
        wanted = [s for s in rows if (not after or s["id"] > after) and int(s["id"][:13]) >= since]
        return {"submissions": wanted[:limit], "total": len(rows),
                "last_7_days": sum(1 for s in rows if int(s["id"][:13]) >= week_ago), "more": len(wanted) > limit}

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeForms(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Netlify Forms at {srv.netlify_url} (token {srv.token}, site {srv.site}); fake forms Worker at "
          f"{srv.worker_url} (secret {srv.secret})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
