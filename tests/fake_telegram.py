"""A fake Telegram Bot API (plus a fake ntfy endpoint) so the phone side of Holdco runs in tests without network.

Same shape as tests/fake_ollama.py: a ThreadingHTTPServer on 127.0.0.1 that records what Holdco sends and lets
a test queue updates (button presses, text commands) for the next getUpdates call.

    tg = FakeTelegram().start()          # tg.url is the telegram_api_base, tg.token the bot token, tg.chat_id 42
    tg.push_callback("a:12:approve", message_id=tg.sent[-1]["message_id"])
    tg.push_message("/status")
    ...; tg.stop()
    python tests/fake_telegram.py 11998  # standalone, prints the URL

Records: sent (sendMessage bodies with the message_id assigned), edits (editMessageReplyMarkup and
editMessageText bodies, with "method"), answered (answerCallbackQuery bodies), ntfy ({"topic", "body", "title",
"priority"}), requests (every Bot API call: {"method", "body"}). `edited` and `push_text` are aliases.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


if __name__ == "__main__":
    srv = FakeTelegram(int(sys.argv[1]) if len(sys.argv) > 1 else 11998)
    print(f"Fake Telegram on {srv.url} (token {srv.token}, chat id {srv.chat_id}); ntfy at {srv.url}/ntfy/<topic>",
          flush=True)
    srv.httpd.serve_forever()
