"""A fake MCP server for tests: both protocol generations, over stdio or Streamable HTTP.

    fake = FakeMCPHttp(port=0, old=False, sse=False, auth_base=None).start()   # fake.url, fake.mcp_url
    ...; fake.stop()
    python tests/fake_mcp_server.py --stdio [--old]
    python tests/fake_mcp_server.py --http PORT [--old] [--sse] [--auth URL]

Tools: echo(text), add(a, b) (structuredContent {"sum": n} and text), fail(text) (isError), image() (a 1x1 PNG
block), slow(seconds), input_required() (a result whose resultType is "input_required").

New mode (2026-07-28, the default): server/discover answers; every request must carry the namespaced _meta keys
protocolVersion and clientCapabilities and, over HTTP, the Mcp-Method header (the JSON-RPC method) and, on a request
that targets a name (tools/call), the Mcp-Name header with that name, else -32600; a version not in `supported`
gets -32022; results carry resultType "complete" and _meta serverInfo; initialize and ping answer -32601; no
session id. Old mode (--old, 2025-11-25): server/discover answers -32601; initialize answers `old_version`;
tools/* before notifications/initialized answer -32600 "not initialized"; ping answers {}; over HTTP initialize
sets Mcp-Session-Id, later requests without it get 400 and with an unknown one 404.

HTTP: POST /mcp (any POST path except the well-known ones is the endpoint). With sse every reply is one
"event: message" / "data: <json>" block. With auth_base, requests need a bearer token that
GET <auth_base>/introspect?token= calls active, else 401 with WWW-Authenticate pointing at
<self>/.well-known/oauth-protected-resource, which names auth_base.

Records: `calls` (every JSON-RPC message received: a (method, params) pair that also answers ["headers"], ["id"],
.headers and .id), `requests` (every HTTP request: method, path, status, headers). Test hooks on FakeMCPHttp:
`supported`, `old_version`, `break_streams` (the next n SSE replies end before the reply), `challenge` (replaces
the WWW-Authenticate value), `discover_status` (answer server/discover with that HTTP status), `expire_sessions()`.
The stdio script exits with "fake-mcp: fatal error ..." on stderr when the tool named in FAKE_MCP_CRASH_ON is called.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def main(argv=None) -> None:
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


if __name__ == "__main__":
    main()
