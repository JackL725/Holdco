"""A fake Resend API, so email through Resend runs in tests without network or a real account.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the Resend connector's `api_base` at `.url`.

    resend = FakeResend(domains=("pawplan.example",)).start()
    ... send through the connector ...
    resend.sent[-1]["body"]["to"], resend.sent[-1]["idempotency_key"]
    resend.stop()
    python tests/fake_resend.py 12002   # standalone, prints the URL

Auth: `Authorization: Bearer re_...`. A key starting with "re_send_" acts as a sending-only key (GET /domains
answers 401 restricted_api_key, sending works); any key not starting with "re_" gets 401.

Endpoints: GET /domains -> {"data": [{"id", "name", "status", "region", "created_at"}]}; each entry of `domains`
is a name (status "verified") or a (name, status) pair. POST /emails (JSON {from, to, subject, text|html,
reply_to, headers}) records the body plus the Idempotency-Key header and returns {"id": "re_<n>"}; a repeat with
the same Idempotency-Key returns the same id without a second record. Missing from, to or subject -> 422.

Records: sent ([{"id", "body", "idempotency_key"}]), requests ([{"method", "path"}]).
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeResend:
    def __init__(self, port: int = 0, domains=("pawplan.example",)):
        self.domains: list[tuple[str, str]] = [(d, "verified") if isinstance(d, str) else (d[0], d[1])
                                               for d in domains]
        self.sent: list[dict] = []
        self.requests: list[dict] = []
        self._by_key: dict[str, str] = {}
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

            def _key(self) -> str:
                auth = self.headers.get("Authorization") or ""
                return auth[7:].strip() if auth.startswith("Bearer ") else ""

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path})
                key = self._key()
                if not key.startswith("re_"):
                    return self._json(401, {"statusCode": 401, "name": "validation_error",
                                            "message": "API key is invalid"})
                if self.path.split("?")[0].rstrip("/") == "/domains":
                    if key.startswith("re_send_"):
                        return self._json(401, {"statusCode": 401, "name": "restricted_api_key",
                                                "message": "This API key is restricted to only send emails"})
                    return self._json(200, {"object": "list", "data": [
                        {"id": f"d_{i}", "name": n, "status": s, "region": "us-east-1",
                         "created_at": "2026-10-01T00:00:00.000Z"} for i, (n, s) in enumerate(outer.domains, 1)]})
                self._json(404, {"statusCode": 404, "name": "not_found", "message": "Route not found"})

            def do_POST(self):
                outer.requests.append({"method": "POST", "path": self.path})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not self._key().startswith("re_"):
                    return self._json(401, {"statusCode": 401, "name": "validation_error",
                                            "message": "API key is invalid"})
                if self.path.split("?")[0].rstrip("/") != "/emails":
                    return self._json(404, {"statusCode": 404, "name": "not_found", "message": "Route not found"})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"statusCode": 400, "name": "invalid_json", "message": "Invalid JSON"})
                for field in ("from", "to", "subject"):
                    if not body.get(field):
                        return self._json(422, {"statusCode": 422, "name": "validation_error",
                                                "message": f"Missing `{field}` field."})
                key = self.headers.get("Idempotency-Key") or ""
                with outer._lock:
                    if key and key in outer._by_key:
                        return self._json(200, {"id": outer._by_key[key]})
                    rid = f"re_{len(outer.sent) + 1}"
                    outer.sent.append({"id": rid, "body": body, "idempotency_key": key or None})
                    if key:
                        outer._by_key[key] = rid
                self._json(200, {"id": rid})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()
            self.requests.clear()
            self._by_key.clear()

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeResend(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Resend at {srv.url} (keys starting re_ are accepted)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
