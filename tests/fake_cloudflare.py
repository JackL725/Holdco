"""A fake Cloudflare API (the Workers static assets deploy), so the Cloudflare connector runs in tests without
network or a real account.

Same shape as tests/fake_resend.py: a ThreadingHTTPServer on 127.0.0.1 with a random port, `.url`, `.start()`,
`.stop()`. Point the Cloudflare connector's `api_base` at `.url` (paths are those under /client/v4).

    cf = FakeCloudflare(account="acc123", subdomain="holdco-test", zones=("pawplan.example",)).start()
    ... deploy through the connector ...
    cf.site("pawplan-site")   # {"/index.html": b"...", ...} as the last deployment serves it
    cf.stop()
    python tests/fake_cloudflare.py 12012   # standalone, prints the URL

Auth: `Authorization: Bearer <token>`; any token is accepted except one starting with "bad" (401, code 1000
"Invalid API Token"). Paths under /accounts/<id>/ need the configured account (403, code 10000 "Authentication
error"). The upload endpoint takes the upload tokens this fake hands out instead.

Endpoints: GET /user/tokens/verify; GET /accounts/<a>/workers/subdomain ({"subdomain"}, 404 when `subdomain` is
empty); POST /accounts/<a>/workers/scripts/<s>/assets-upload-session {"manifest": {path: {hash, size}}} -> {"jwt",
"buckets"} (files it already has are not asked for; `bucket_size` per bucket; nothing to upload answers the
completion token at once); POST /accounts/<a>/workers/assets/upload?base64=true (multipart, parts named by hash,
base64 bodies) -> 202 {"jwt": null} until the session's last file, then 201 {"jwt": <completion token>}; PUT
/accounts/<a>/workers/scripts/<s> (multipart: metadata with assets.jwt = that completion token, and the module) ->
a deployment; POST .../scripts/<s>/subdomain {"enabled"}; GET .../scripts/<s>/deployments (404 code 10007 for an
unknown Worker); GET /zones?name=<n>; PUT /accounts/<a>/workers/domains {hostname, service, zone_id, environment}.

Records: requests ([{"method", "path", "auth"}]), deployments ([{"script", "manifest", "metadata", "module",
"created_on", "id"}]), uploaded (hashes in upload order), domains ([body]), subdomain_enabled ({script: bool}).
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import email
import email.policy
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_multipart(content_type: str, raw: bytes) -> list[dict]:
    """[{"name", "filename", "type", "data"}] of a multipart/form-data body (the email parser, as cgi is gone)."""
    msg = email.message_from_bytes(f"Content-Type: {content_type}\r\n\r\n".encode() + raw, policy=email.policy.HTTP)
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.iter_parts():
        out.append({"name": part.get_param("name", header="content-disposition"),
                    "filename": part.get_param("filename", header="content-disposition"),
                    "type": part.get_content_type(), "data": part.get_payload(decode=True) or b""})
    return out


class FakeCloudflare:
    def __init__(self, port: int = 0, account: str = "acc123", subdomain: str = "holdco-test",
                 zones=("pawplan.example",), bucket_size: int = 2):
        self.account = account
        self.subdomain = subdomain
        self.zones = {z: f"zone-{i}" for i, z in enumerate(zones, 1)}
        self.bucket_size = bucket_size
        self.store: dict[str, bytes] = {}          # hash -> file content (decoded)
        self.uploaded: list[str] = []
        self.deployments: list[dict] = []
        self.domains: list[dict] = []
        self.subdomain_enabled: dict[str, bool] = {}
        self.requests: list[dict] = []
        self._sessions: dict[str, dict] = {}       # upload token -> {"script", "manifest", "pending"}
        self._completions: dict[str, dict] = {}    # completion token -> {"script", "manifest"}
        self._n = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code: int, result=None, errors=None):
                obj = {"success": not errors and code < 400, "errors": errors or [], "messages": [],
                       "result": result}
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _handle(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                parsed = urllib.parse.urlsplit(self.path)
                path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
                auth = self.headers.get("Authorization") or ""
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": path, "auth": token})
                parts = [p for p in path.split("/") if p]
                if path.endswith("/workers/assets/upload"):
                    return self._upload(token, raw)
                if not token or token.startswith("bad"):
                    return self._json(401, errors=[{"code": 1000, "message": "Invalid API Token"}])
                if path == "/user/tokens/verify" and self.command == "GET":
                    return self._json(200, {"id": "tok-1", "status": "active"})
                if path == "/zones" and self.command == "GET":
                    name = (query.get("name") or [""])[0]
                    return self._json(200, [{"id": outer.zones[name], "name": name}] if name in outer.zones else [])
                if parts[:1] != ["accounts"] or len(parts) < 2:
                    return self._json(404, errors=[{"code": 7003, "message": "No route for that URI"}])
                if parts[1] != outer.account:
                    return self._json(403, errors=[{"code": 10000, "message": "Authentication error"}])
                rest = parts[2:]
                if rest == ["tokens", "verify"]:
                    return self._json(200, {"id": "tok-1", "status": "active"})
                if rest == ["workers", "subdomain"]:
                    if not outer.subdomain:
                        return self._json(404, errors=[{"code": 10007, "message": "No subdomain"}])
                    return self._json(200, {"subdomain": outer.subdomain})
                if rest == ["workers", "domains"] and self.command == "PUT":
                    body = json.loads(raw or b"{}")
                    with outer._lock:
                        outer.domains.append(body)
                    return self._json(200, {"id": f"dom-{len(outer.domains)}", **body})
                if len(rest) >= 3 and rest[:2] == ["workers", "scripts"]:
                    return self._script(rest[2], rest[3:], raw)
                return self._json(404, errors=[{"code": 7003, "message": "No route for that URI"}])

            def _script(self, script: str, tail: list, raw: bytes):
                if tail == ["assets-upload-session"] and self.command == "POST":
                    manifest = (json.loads(raw or b"{}") or {}).get("manifest") or {}
                    with outer._lock:
                        need = sorted({v["hash"] for v in manifest.values() if v["hash"] not in outer.store})
                        outer._n += 1
                        if not need:
                            jwt = f"jwt-complete-{outer._n}"
                            outer._completions[jwt] = {"script": script, "manifest": manifest}
                            return self._json(200, {"jwt": jwt, "buckets": []})
                        jwt = f"jwt-session-{outer._n}"
                        outer._sessions[jwt] = {"script": script, "manifest": manifest, "pending": set(need)}
                    size = max(1, outer.bucket_size)
                    buckets = [need[i:i + size] for i in range(0, len(need), size)]
                    return self._json(200, {"jwt": jwt, "buckets": buckets})
                if tail == [] and self.command == "PUT":
                    parts = parse_multipart(self.headers.get("Content-Type") or "", raw)
                    meta = next((p for p in parts if p["name"] == "metadata"), None)
                    if meta is None:
                        return self._json(400, errors=[{"code": 10021, "message": "metadata part missing"}])
                    metadata = json.loads(meta["data"] or b"{}")
                    jwt = ((metadata.get("assets") or {}).get("jwt")) or ""
                    module = next((p for p in parts if p["name"] == metadata.get("main_module")), None)
                    with outer._lock:
                        done = outer._completions.get(jwt)
                        if not done or done["script"] != script:
                            return self._json(400, errors=[{"code": 10022, "message": "Invalid assets token"}])
                        outer._n += 1
                        dep = {"id": f"dep-{outer._n}", "script": script, "manifest": done["manifest"],
                               "metadata": metadata, "module": (module or {}).get("data", b"").decode(),
                               "created_on": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
                        outer.deployments.append(dep)
                    return self._json(200, {"id": script, "created_on": dep["created_on"]})
                if tail == ["subdomain"] and self.command == "POST":
                    body = json.loads(raw or b"{}")
                    with outer._lock:
                        outer.subdomain_enabled[script] = bool(body.get("enabled"))
                    return self._json(200, {"enabled": bool(body.get("enabled"))})
                if tail == ["deployments"] and self.command == "GET":
                    with outer._lock:
                        deps = [d for d in outer.deployments if d["script"] == script]
                    if not deps:
                        return self._json(404, errors=[{"code": 10007,
                                                        "message": "This Worker does not exist on your account."}])
                    return self._json(200, {"deployments": [
                        {"id": d["id"], "created_on": d["created_on"], "source": "api", "strategy": "percentage",
                         "versions": [{"version_id": d["id"], "percentage": 100}]} for d in reversed(deps)]})
                return self._json(404, errors=[{"code": 7003, "message": "No route for that URI"}])

            def _upload(self, token: str, raw: bytes):
                with outer._lock:
                    session = outer._sessions.get(token)
                    if session is None:
                        return self._json(401, errors=[{"code": 10001, "message": "Invalid upload token"}])
                    for part in parse_multipart(self.headers.get("Content-Type") or "", raw):
                        h = part["name"]
                        if h not in session["pending"]:
                            return self._json(400, errors=[{"code": 10023, "message": f"Unexpected file {h}"}])
                        try:
                            outer.store[h] = base64.b64decode(part["data"], validate=True)
                        except (binascii.Error, ValueError):
                            return self._json(400, errors=[{"code": 10024, "message": "Not base64"}])
                        session["pending"].discard(h)
                        outer.uploaded.append(h)
                    if session["pending"]:
                        return self._json(202, {"jwt": None})
                    outer._n += 1
                    jwt = f"jwt-complete-{outer._n}"
                    outer._completions[jwt] = {"script": session["script"], "manifest": session["manifest"]}
                    outer._sessions.pop(token, None)
                return self._json(201, {"jwt": jwt})

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def site(self, script: str) -> dict[str, bytes]:
        """The files the Worker's last deployment serves, by path."""
        with self._lock:
            dep = next((d for d in reversed(self.deployments) if d["script"] == script), None)
            if dep is None:
                return {}
            return {p: self.store.get(v["hash"], b"") for p, v in dep["manifest"].items()}

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeCloudflare(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Cloudflare at {srv.url} (account acc123; any token not starting with 'bad')")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
