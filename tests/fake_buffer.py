"""A fake Buffer GraphQL API, so the Buffer connector and social posts through it run in tests without network.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the Buffer connector's `api_base` at `.url`.

    buffer = FakeBuffer().start()          # buffer.token is the accepted Bearer token
    ... list channels, create posts ...
    buffer.posts[-1]["input"]["channelId"]
    buffer.stop()
    python tests/fake_buffer.py 12003      # standalone, prints the URL

`POST /` with a JSON {"query", "variables"} body and `Authorization: Bearer <token>` (anything else gets 401):
a query mentioning createPost records the post and returns {"post": {"id", "text", "dueAt"}}; one mentioning
channels returns two channels (services "x" and "linkedin"); one mentioning account returns the account with one
organization. Buffer's real API is in beta, so this only mirrors the shapes Holdco sends.

Records: posts ([{"id", "input", "query"}]), queries (every request body). Test hooks: `channels` (the list the
channels query returns; tests may add to it) and `fail_channels` (createPost for these ids answers an error).
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHANNELS = [{"id": "chan_x", "name": "pawplan", "displayName": "PawPlan on X", "service": "x"},
            {"id": "chan_li", "name": "pawplan", "displayName": "PawPlan on LinkedIn", "service": "linkedin"}]


class FakeBuffer:
    def __init__(self, port: int = 0, token: str = "buffer-test-token"):
        self.token = token
        self.channels = [dict(c) for c in CHANNELS]
        self.fail_channels: set[str] = set()   # test hook: createPost for these channel ids answers an error
        self.posts: list[dict] = []
        self.queries: list[dict] = []
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
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if (self.headers.get("Authorization") or "") != f"Bearer {outer.token}":
                    return self._json(401, {"errors": [{"message": "Unauthorized",
                                                        "extensions": {"code": "UNAUTHENTICATED"}}]})
                if self.path.split("?")[0] not in ("/", ""):
                    return self._json(404, {"errors": [{"message": "Not found"}]})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"errors": [{"message": "Invalid JSON"}]})
                query = str(body.get("query") or "")
                variables = body.get("variables") or {}
                with outer._lock:
                    outer.queries.append(body)
                    if "createPost" in query:
                        post_input = variables.get("input") or {}
                        if not post_input.get("channelId") or not post_input.get("text"):
                            return self._json(200, {"data": {"createPost": {
                                "message": "channelId and text are required"}}})
                        if post_input.get("channelId") in outer.fail_channels:
                            return self._json(200, {"data": {"createPost": {
                                "message": "The channel needs to be reconnected"}}})
                        pid = f"post_{len(outer.posts) + 1}"
                        outer.posts.append({"id": pid, "input": post_input, "query": query})
                        return self._json(200, {"data": {"createPost": {"post": {
                            "id": pid, "text": post_input.get("text"), "dueAt": post_input.get("dueAt")}}}})
                    if "channels" in query:
                        return self._json(200, {"data": {"channels": outer.channels}})
                    if "account" in query:
                        return self._json(200, {"data": {"account": {
                            "id": "acct_1", "email": "owner@pawplan.example",
                            "organizations": [{"id": "org_1", "name": "PawPlan"}]}}})
                self._json(200, {"errors": [{"message": "Unknown query"}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def clear(self) -> None:
        with self._lock:
            self.posts.clear()
            self.queries.clear()

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeBuffer(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Buffer at {srv.url} (token {srv.token})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
