"""A fake OAuth authorization server for tests: metadata, registration, authorize (no human step), token, introspect.

    auth = FakeOAuth(port=0, expires_in=3600, wrong_iss=False).start()   # auth.url is also the issuer
    ...; auth.stop()
    python tests/fake_oauth.py 11997   # standalone, prints the URL

GET /.well-known/oauth-authorization-server and /.well-known/openid-configuration: the metadata (the first answers
404 with oauth_metadata=False, to exercise the OpenID fallback). POST /register: {"client_id": "fake-client"}
(404 and no registration_endpoint in the metadata with registration=False). GET /authorize checks client_id,
redirect_uri, state, code_challenge, code_challenge_method=S256 and resource, stores the challenge and answers 302 to
<redirect_uri>?code=&state=&iss= (with wrong_iss=True the iss is another origin). POST /token: authorization_code
(the code_verifier must match the stored S256 challenge) or refresh_token (rotated); returns access_token,
token_type Bearer, expires_in and refresh_token. GET /introspect?token= answers {"active": bool} (expired tokens are
inactive). Records: tokens (issued), registrations, authorizations, token_requests, requests (method, path).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WRONG_ISSUER = "https://wrong-issuer.example"


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


class FakeOAuth:
    def __init__(self, port: int = 0, expires_in: int = 3600, wrong_iss: bool = False, registration: bool = True,
                 oauth_metadata: bool = True):
        self.expires_in = expires_in
        self.wrong_iss = wrong_iss
        self.registration = registration
        self.oauth_metadata = oauth_metadata
        self.client_ids = {"fake-client"}   # clients /authorize and /token accept; /register hands out fake-client
        self.tokens: list[dict] = []
        self.registrations: list[dict] = []
        self.authorizations: list[dict] = []
        self.token_requests: list[dict] = []
        self.requests: list[tuple] = []
        self._codes: dict[str, dict] = {}
        self._access: dict[str, float] = {}     # access token -> expiry (epoch)
        self._refresh: dict[str, dict] = {}     # refresh token -> {client_id, resource}
        self._redirects: dict[str, set] = {}    # client id -> registered redirect URIs
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code, obj, extra=None):
                self._send(code, json.dumps(obj).encode(), "application/json", extra)

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                outer.requests.append(("GET", path))
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                if path == "/.well-known/oauth-authorization-server":
                    if not outer.oauth_metadata:
                        return self._json(404, {"error": "not found"})
                    return self._json(200, outer.metadata())
                if path == "/.well-known/openid-configuration":
                    return self._json(200, {**outer.metadata(), "subject_types_supported": ["public"],
                                            "id_token_signing_alg_values_supported": ["RS256"]})
                if path == "/authorize":
                    return outer._authorize(self, query)
                if path == "/introspect":
                    with outer._lock:
                        expiry = outer._access.get(query.get("token", ""))
                    return self._json(200, {"active": expiry is not None and expiry > time.time()})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                outer.requests.append(("POST", path))
                raw = self._body()
                if path == "/register":
                    if not outer.registration:
                        return self._json(404, {"error": "not found"})
                    try:
                        body = json.loads(raw.decode("utf-8") or "{}")
                    except ValueError:
                        return self._json(400, {"error": "invalid_client_metadata"})
                    uris = body.get("redirect_uris") if isinstance(body, dict) else None
                    if not isinstance(uris, list) or not uris:
                        return self._json(400, {"error": "invalid_redirect_uri",
                                                "error_description": "redirect_uris is required"})
                    outer.registrations.append(body)
                    with outer._lock:
                        outer._redirects.setdefault("fake-client", set()).update(uris)
                    return self._json(201, {"client_id": "fake-client", "client_id_issued_at": int(time.time()),
                                            "redirect_uris": uris, "token_endpoint_auth_method": "none",
                                            "grant_types": ["authorization_code", "refresh_token"]})
                if path == "/token":
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
                    outer.token_requests.append(form)
                    code, obj = outer._token(form)
                    return self._json(code, obj)
                return self._json(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.issuer = self.url
        self._thread: threading.Thread | None = None

    def metadata(self) -> dict:
        m = {"issuer": self.issuer, "authorization_endpoint": self.url + "/authorize",
             "token_endpoint": self.url + "/token", "response_types_supported": ["code"],
             "code_challenge_methods_supported": ["S256"],
             "grant_types_supported": ["authorization_code", "refresh_token"],
             "token_endpoint_auth_methods_supported": ["none"],
             "authorization_response_iss_parameter_supported": True}
        if self.registration:
            m["registration_endpoint"] = self.url + "/register"
        return m

    # -- the flow --

    def _authorize(self, handler, q: dict):
        self.authorizations.append(dict(q))
        problem = None
        redirect = q.get("redirect_uri", "")
        parts = urllib.parse.urlsplit(redirect)
        if q.get("response_type") != "code":
            problem = "response_type must be code"
        elif q.get("client_id") not in self.client_ids:
            problem = "unknown client_id"
        elif parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
            problem = "redirect_uri must be a loopback http URL"
        elif q.get("client_id") in self._redirects and redirect not in self._redirects[q["client_id"]]:
            problem = "redirect_uri was not registered"
        elif not q.get("state"):
            problem = "state is required"
        elif not q.get("code_challenge"):
            problem = "code_challenge is required"
        elif q.get("code_challenge_method") != "S256":
            problem = "code_challenge_method must be S256"
        elif not q.get("resource"):
            problem = "resource is required"
        if problem:
            return handler._send(400, f"invalid_request: {problem}".encode(), "text/plain; charset=utf-8")
        code = "code-" + secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = {"challenge": q["code_challenge"], "client_id": q["client_id"],
                                 "redirect_uri": redirect, "resource": q["resource"]}
        iss = WRONG_ISSUER if self.wrong_iss else self.issuer
        location = redirect + ("&" if "?" in redirect else "?") + \
            urllib.parse.urlencode({"code": code, "state": q["state"], "iss": iss})
        return handler._send(302, b"", "text/plain", {"Location": location})

    def _token(self, form: dict) -> tuple[int, dict]:
        grant = form.get("grant_type")
        with self._lock:
            if grant == "authorization_code":
                entry = self._codes.pop(form.get("code", ""), None)
                if entry is None:
                    return 400, {"error": "invalid_grant", "error_description": "unknown or used code"}
                if form.get("client_id") != entry["client_id"] or form.get("redirect_uri") != entry["redirect_uri"]:
                    return 400, {"error": "invalid_grant", "error_description": "client_id or redirect_uri differs"}
                if s256(form.get("code_verifier", "")) != entry["challenge"]:
                    return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
                return 200, self._issue(grant, entry["client_id"], entry["resource"])
            if grant == "refresh_token":
                entry = self._refresh.pop(form.get("refresh_token", ""), None)
                if entry is None or form.get("client_id") != entry["client_id"]:
                    return 400, {"error": "invalid_grant", "error_description": "unknown refresh token"}
                return 200, self._issue(grant, entry["client_id"], entry["resource"])
        return 400, {"error": "unsupported_grant_type"}

    def _issue(self, grant: str, client_id: str, resource: str) -> dict:
        access = "fake-at-" + secrets.token_urlsafe(18)
        refresh = "fake-rt-" + secrets.token_urlsafe(18)
        self._access[access] = time.time() + self.expires_in
        self._refresh[refresh] = {"client_id": client_id, "resource": resource}
        self.tokens.append({"access_token": access, "refresh_token": refresh, "grant_type": grant,
                            "expires_in": self.expires_in, "client_id": client_id, "resource": resource})
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                "refresh_token": refresh}

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(5)


if __name__ == "__main__":
    server = FakeOAuth(port=int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake OAuth server at {server.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
