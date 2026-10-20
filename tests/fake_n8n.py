"""A fake n8n webhook, so the n8n connector runs in tests without network or a real n8n.

Same shape as tests/fake_resend.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the n8n card's webhook_url at `.hook_url` (`<url>/webhook/holdco`).

    n8n = FakeN8n(secret="s3cret-value").start()
    ... trigger through the connector ...
    n8n.received[-1]["body"], n8n.received[-1]["headers"]["x-holdco-secret"]
    n8n.stop()
    python tests/fake_n8n.py 12013 [secret]   # standalone, prints the URL

Behaviour, like a Webhook node with Header Auth: POST /webhook/<path> needs the X-Holdco-Secret header to equal
`secret` (when one is set), else HTTP 403 "Authorization data is wrong!"; an unknown path (anything but `path`) is
404 like an inactive workflow. A good request is recorded and answered with `status` (default 200) and `answer`
(default n8n's {"message": "Workflow was started"}).

Records: received ([{"path", "headers" (lower-case keys), "body"}]) for every request that reached the workflow,
refused (count of 403s).
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeN8n:
    def __init__(self, port: int = 0, secret: str = "", path: str = "holdco"):
        self.secret = secret
        self.path = path
        self.status = 200
        self.answer: dict = {"message": "Workflow was started"}
        self.received: list[dict] = []
        self.refused = 0
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

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.split("?", 1)[0].rstrip("/") != f"/webhook/{outer.path}":
                    return self._json(404, {"code": 404, "message": "The requested webhook is not registered."})
                if outer.secret and self.headers.get("X-Holdco-Secret") != outer.secret:
                    with outer._lock:
                        outer.refused += 1
                    return self._json(403, {"message": "Authorization data is wrong!"})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with outer._lock:
                    outer.received.append({"path": self.path, "body": body,
                                           "headers": {k.lower(): v for k, v in self.headers.items()}})
                self._json(outer.status, outer.answer)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.hook_url = f"{self.url}/webhook/{path}"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeN8n(int(sys.argv[1]) if len(sys.argv) > 1 else 0, sys.argv[2] if len(sys.argv) > 2 else "").start()
    print(f"Fake n8n webhook at {srv.hook_url}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
