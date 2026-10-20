"""A fake Porkbun API v3, so the Porkbun connector runs in tests without network or a real account.

Same shape as tests/fake_resend.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the Porkbun connector's `api_base` at `.url`.

    porkbun = FakePorkbun(owned=("pawplan.example",), taken=("taken.example",)).start()
    ... call the connector ...
    porkbun.registered, porkbun.calls("/domain/create/pawplan.co")[-1]["idempotency_key"]
    porkbun.stop()
    python tests/fake_porkbun.py 12011   # standalone, prints the URL

Auth: the X-API-Key header must start with "pk1_" and X-Secret-API-Key with "sk1_", else HTTP 400 {"status":
"ERROR", "message": "Invalid API key. (002)"}. Every answer carries "sandbox": true when the API key starts with
"pk1_sb_"; `sandbox_flag` (True or False) overrides that for every key.

Endpoints (all POST, JSON bodies): /ping; /domain/checkDomain/<d> (available unless owned or taken; the price is
`prices[tld]`, default 9.73, renewal the same); /domain/listAll; /dns/retrieve/<d>; /dns/retrieveByNameType/<d>/
<type>[/<sub>]; /dns/create/<d> {name, type, content, ttl, prio}; /dns/editByNameType/<d>/<type>[/<sub>] {content,
ttl, prio}; /domain/create/<d> {cost (cents, must equal the price), agreeToTerms: "yes"}. DNS paths need an owned
domain. A body with "dryRun": true changes nothing and answers {"dryRun": true, "would": ...}.

Idempotency: a write (dns/create, dns/editByNameType, domain/create) with an Idempotency-Key that was seen before
is not applied again: the first answer is replayed (`replays` counts them). Test hooks: `fail_after_apply` (the next
N writes are applied and remembered, then answered HTTP 502, as when the reply is lost), `rate_limit` (the next N
checkDomain calls answer the rate-limit error).

Records: requests ([{"path", "headers" (lower-case keys), "body", "idempotency_key"}]), registered ([{"domain",
"cost", "years"}]), dns ({domain: [records]}).
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WRITES = ("/dns/create/", "/dns/editByNameType/", "/domain/create/")


class FakePorkbun:
    def __init__(self, port: int = 0, owned=("pawplan.example",), taken=(), prices: dict | None = None):
        self.owned: list[str] = list(owned)
        self.taken: set[str] = set(taken)
        self.prices: dict[str, str] = {"co": "10.81", "com": "11.08", **(prices or {})}
        self.dns: dict[str, list[dict]] = {d: [] for d in self.owned}
        self.registered: list[dict] = []
        self.requests: list[dict] = []
        self.sandbox_flag: bool | None = None
        self.fail_after_apply = 0
        self.rate_limit = 0
        self.replays = 0
        self._answers: dict[str, tuple[int, dict]] = {}
        self._next_id = 1000
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code: int, obj: dict):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self._json(405, {"status": "ERROR", "message": "Use POST."})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"status": "ERROR", "message": "Invalid JSON."})
                path = self.path.split("?", 1)[0]
                key = self.headers.get("X-API-Key") or ""
                ikey = self.headers.get("Idempotency-Key") or ""
                with outer._lock:
                    outer.requests.append({"path": path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                           "body": body, "idempotency_key": ikey or None})
                if not key.startswith("pk1_") or not (self.headers.get("X-Secret-API-Key") or "").startswith("sk1_"):
                    return self._json(400, {"status": "ERROR", "message": "Invalid API key. (002)"})
                sandbox = outer.sandbox_flag if outer.sandbox_flag is not None else key.startswith("pk1_sb_")
                write = path.startswith(WRITES)
                with outer._lock:
                    if write and ikey and ikey in outer._answers:
                        outer.replays += 1
                        code, answer = outer._answers[ikey]
                        return self._json(code, answer)
                    code, answer = outer._route(path, body)
                    if sandbox:
                        answer["sandbox"] = True
                    lose = False
                    if write and code < 400:
                        if ikey:
                            outer._answers[ikey] = (code, answer)
                        if outer.fail_after_apply > 0:
                            outer.fail_after_apply -= 1
                            lose = True
                if lose:
                    return self._json(502, {"status": "ERROR", "message": "Bad gateway"})
                self._json(code, answer)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    # ---------------------------------------------------------------- behaviour (called with the lock held)

    def _price(self, domain: str) -> str:
        return self.prices.get(domain.rsplit(".", 1)[-1], "9.73")

    def _available(self, domain: str) -> bool:
        return domain not in self.owned and domain not in self.taken

    def _route(self, path: str, body: dict) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        ok = {"status": "SUCCESS"}
        if path == "/ping":
            return 200, {**ok, "yourIp": "127.0.0.1"}
        if path.startswith("/domain/checkDomain/") and len(parts) == 3:
            if self.rate_limit > 0:
                self.rate_limit -= 1
                return 400, {"status": "ERROR", "message": "Domain checks are rate limited: 1 per 10 seconds."}
            d, price = parts[2], self._price(parts[2])
            return 200, {**ok, "response": {
                "avail": "yes" if self._available(d) else "no", "type": "registration", "price": price,
                "firstYearPromo": "no", "regularPrice": price, "premium": "no",
                "additional": {"renewal": {"type": "renewal", "price": price, "regularPrice": price},
                               "transfer": {"type": "transfer", "price": price, "regularPrice": price}}},
                "limits": {"TTL": "10 seconds", "limit": "1", "used": 1}}
        if path == "/domain/listAll":
            return 200, {**ok, "domains": [
                {"domain": d, "status": "ACTIVE", "tld": d.rsplit(".", 1)[-1], "createDate": "2026-10-01 00:00:00",
                 "expireDate": "2027-10-01 00:00:00", "securityLock": "1", "whoisPrivacy": "1", "autoRenew": 1,
                 "notLocal": 0} for d in self.owned]}
        if path.startswith("/domain/create/") and len(parts) == 3:
            d = parts[2]
            if not self._available(d):
                return 400, {"status": "ERROR", "message": f"{d} is not available."}
            years = int(body.get("years") or 1)
            cents = int(round(float(self._price(d)) * 100)) * years
            if str(body.get("agreeToTerms")).lower() != "yes":
                return 400, {"status": "ERROR", "message": "You must agree to the terms."}
            if int(body.get("cost") or -1) != cents:
                return 400, {"status": "ERROR", "message": f"The cost does not match the price ({cents})."}
            if body.get("dryRun") is True:
                return 200, {**ok, "dryRun": True, "domain": d, "cost": cents, "would": "register"}
            self.owned.append(d)
            self.dns[d] = []
            self.registered.append({"domain": d, "cost": cents, "years": years})
            self._next_id += 1
            return 200, {**ok, "domain": d, "cost": cents, "orderId": self._next_id}
        if parts[:1] == ["dns"] and len(parts) >= 3:
            d = parts[2]
            if d not in self.owned:
                return 400, {"status": "ERROR", "message": "Invalid domain."}
            records = self.dns.setdefault(d, [])
            sub = parts[4] if len(parts) > 4 else ""
            fqdn = f"{sub}.{d}" if sub else d
            if parts[1] == "retrieve":
                return 200, {**ok, "records": records}
            if parts[1] == "retrieveByNameType" and len(parts) >= 4:
                return 200, {**ok, "records": [r for r in records if r["type"] == parts[3] and r["name"] == fqdn]}
            if parts[1] == "create":
                sub = str(body.get("name") or "")
                rec = {"name": f"{sub}.{d}" if sub else d, "type": str(body.get("type") or ""),
                       "content": str(body.get("content") or ""), "ttl": str(body.get("ttl") or "600"),
                       "prio": str(body.get("prio") or "0"), "notes": ""}
                if body.get("dryRun") is True:
                    return 200, {**ok, "dryRun": True, "would": {"create": rec}}
                self._next_id += 1
                rec["id"] = str(self._next_id)
                records.append(rec)
                return 200, {**ok, "id": rec["id"]}
            if parts[1] == "editByNameType" and len(parts) >= 4:
                hits = [r for r in records if r["type"] == parts[3] and r["name"] == fqdn]
                if body.get("dryRun") is True:
                    return 200, {**ok, "dryRun": True, "would": {"edit": len(hits)}}
                for r in hits:
                    r.update(content=str(body.get("content") or ""), ttl=str(body.get("ttl") or "600"))
                return 200, ok
        return 404, {"status": "ERROR", "message": "Unknown endpoint."}

    # ---------------------------------------------------------------- helpers for tests

    def calls(self, prefix: str = "") -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"].startswith(prefix)]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakePorkbun(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Porkbun at {srv.url} (keys pk1_... and sk1_...; pk1_sb_ keys are sandbox)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
