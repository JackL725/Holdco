"""OAuth for hosted MCP servers (Phase 1, docs/phases/phase-1-connect.md section 3).

Authorization code with PKCE (S256) and a random state. The owner presses Sign in on a connector card:

    url = oauth.start(connector_id)        # the dashboard opens it in a new tab
    # the provider sends the browser back to http://127.0.0.1:<port>/oauth/callback?code=&state=&iss=
    code, html = oauth.callback_page(query)   # finish(): exchanges the code, keeps the tokens in the vault
    client = mcp.Client(..., auth=oauth.TokenSource(connector_id))   # supplies and refreshes the bearer token

Discovery: protected resource metadata (RFC 9728), then authorization server metadata (RFC 8414) falling back to
the OpenID configuration, then dynamic client registration (RFC 7591) when offered, else the connector's
oauth_client_id. Token sets live in the vault as oauth.<connector id>; tokens are never logged or shown.
"""
from __future__ import annotations

import base64
import hashlib
import html
import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config, db, vault

PENDING_TTL = 600      # seconds a started sign-in stays valid
REFRESH_MARGIN = 60    # refresh the access token when fewer seconds than this remain
HTTP_TIMEOUT = 30

_meta: dict[str, dict] = {}            # issuer -> authorization server metadata (cached)
_registered: dict[tuple, str] = {}     # (issuer, redirect URI) -> client_id from dynamic registration
_pending: dict[str, dict] = {}         # state -> a sign-in waiting for its callback
_refresh_locks: dict[int, threading.Lock] = {}
_lock = threading.Lock()


class OAuthError(Exception):
    """A sign-in problem. needs_sign_in is True when signing in again is the fix."""

    def __init__(self, message: str, needs_sign_in: bool = False):
        super().__init__(message)
        self.needs_sign_in = needs_sign_in


# ---------------- small helpers ----------------

def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/oauth/callback"


def _origin(url: str) -> str:
    """scheme://host[:port], lower case, without the default port, so equal origins compare equal."""
    p = urllib.parse.urlsplit(url or "")
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = p.port
    except ValueError:
        port = None
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _is_loopback(url: str) -> bool:
    return (urllib.parse.urlsplit(url or "").hostname or "") in ("127.0.0.1", "localhost", "::1")


def _require_https(url: str, what: str) -> None:
    scheme = urllib.parse.urlsplit(url or "").scheme.lower()
    if scheme == "https" or (scheme == "http" and _is_loopback(url)):
        return
    raise OAuthError(f"{what} must use https ({url or 'missing'}).")


def _same_issuer(a: str | None, b: str | None) -> bool:
    return (a or "").rstrip("/") == (b or "").rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Local servers (and the test fakes) never go through a proxy. Posts that carry codes or tokens never follow redirects.
_openers = {
    (True, True): urllib.request.build_opener(urllib.request.ProxyHandler({})),
    (True, False): urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect),
    (False, True): urllib.request.build_opener(),
    (False, False): urllib.request.build_opener(_NoRedirect),
}


def _http(url: str, data: bytes | None = None, content_type: str | None = None) -> tuple[int, bytes]:
    headers = {"Accept": "application/json", "User-Agent": f"Holdco/{__version__}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    opener = _openers[(_is_loopback(url), data is None)]
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read() or b""
        finally:
            e.close()
    except urllib.error.URLError as e:
        raise OAuthError(f"Cannot reach {url}: {e.reason}") from None
    except (OSError, http.client.HTTPException) as e:
        raise OAuthError(f"Cannot reach {url}: {e}") from None


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _get_json(url: str) -> dict | None:
    status, raw = _http(url)
    data = _json(raw) if status == 200 else None
    return data if isinstance(data, dict) else None


def _error_text(data) -> str:
    """What an authorization server said went wrong: only its error fields, never the raw body."""
    if isinstance(data, dict) and data.get("error"):
        desc = str(data.get("error_description") or "")[:200]
        return str(data["error"])[:80] + (f" ({desc})" if desc else "")
    return "an unexpected reply"


# ---------------- discovery and registration ----------------

def _probe(resource: str) -> tuple[str | None, str | None]:
    """Ask the MCP server without a token: its 401 names the protected resource metadata URL (and maybe a scope)."""
    from . import mcp
    client = mcp.Client("sign-in check", "http", url=resource)
    try:
        client.connect()
    except mcp.AuthRequired as e:
        return e.resource_metadata_url, e.scope
    except mcp.MCPError:
        return None, None
    finally:
        client.close()
    return None, None


def _resource_metadata(resource: str, hinted: str | None) -> dict | None:
    """RFC 9728: the URL from WWW-Authenticate, then the well-known URL with the path, then without."""
    origin = _origin(resource)
    path = urllib.parse.urlsplit(resource).path.rstrip("/")
    candidates = ([hinted] if hinted else []) + ([f"{origin}/.well-known/oauth-protected-resource{path}"]
                                                 if path else []) + [f"{origin}/.well-known/oauth-protected-resource"]
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        _require_https(url, "The protected resource metadata")
        try:
            data = _get_json(url)
        except OAuthError:
            data = None
        if data and isinstance(data.get("authorization_servers"), list) and data["authorization_servers"]:
            return data
    return None


def _as_metadata(issuer: str) -> dict:
    """RFC 8414 metadata, falling back to the OpenID configuration; cached per issuer in _meta."""
    with _lock:
        if issuer in _meta:
            return _meta[issuer]
    _require_https(issuer, "The authorization server")
    origin = _origin(issuer)
    path = urllib.parse.urlsplit(issuer).path.rstrip("/")
    if path:
        candidates = [f"{origin}/.well-known/oauth-authorization-server{path}",
                      f"{origin}/.well-known/openid-configuration{path}",
                      f"{issuer.rstrip('/')}/.well-known/openid-configuration"]
    else:
        candidates = [f"{origin}/.well-known/oauth-authorization-server",
                      f"{origin}/.well-known/openid-configuration"]
    meta, last = None, ""
    for url in candidates:
        try:
            data = _get_json(url)
        except OAuthError as e:
            data, last = None, str(e)
        if data and data.get("authorization_endpoint") and data.get("token_endpoint"):
            meta = data
            break
    if meta is None:
        raise OAuthError(f"Could not find the sign-in settings of {issuer}" + (f": {last}" if last else "."))
    if meta.get("issuer") and not _same_issuer(meta["issuer"], issuer):
        raise OAuthError(f"The sign-in settings at {issuer} name a different issuer ({meta['issuer']}); "
                         "Holdco will not use them.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if meta.get(key):
            _require_https(meta[key], f"The {key.replace('_', ' ')}")
    methods = meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and methods and "S256" not in methods:
        raise OAuthError(f"{issuer} does not support PKCE with S256, which Holdco requires.")
    meta = {**meta, "issuer": meta.get("issuer") or issuer}
    with _lock:
        _meta[issuer] = meta
    return meta


def _discover(resource: str) -> dict:
    """{"issuer", "meta", "scope"} for an MCP server URL."""
    hinted, scope = _probe(resource)
    prm = _resource_metadata(resource, hinted)
    if prm:
        if prm.get("resource") and _origin(str(prm["resource"])) != _origin(resource):
            raise OAuthError(f"The sign-in metadata of {_origin(resource)} describes another site "
                             f"({prm['resource']}); Holdco will not use it.")
        issuer = str(prm["authorization_servers"][0])
        if not scope and isinstance(prm.get("scopes_supported"), list):
            scope = " ".join(str(s) for s in prm["scopes_supported"]) or None
    else:
        issuer = _origin(resource)   # older servers run the authorization server on their own origin
    meta = _as_metadata(issuer)
    return {"issuer": meta["issuer"], "meta": meta, "scope": scope}


def _register(meta: dict, issuer: str) -> str:
    """Dynamic client registration (RFC 7591), once per issuer and redirect URI."""
    key = (issuer, redirect_uri())
    with _lock:
        if key in _registered:
            return _registered[key]
    body = {"client_name": "Holdco", "redirect_uris": [redirect_uri()],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    status, raw = _http(meta["registration_endpoint"], json.dumps(body).encode("utf-8"), "application/json")
    data = _json(raw)
    if status not in (200, 201) or not isinstance(data, dict) or not data.get("client_id"):
        raise OAuthError(f"Automatic registration with {issuer} failed (HTTP {status}: {_error_text(data)}).")
    with _lock:
        _registered[key] = str(data["client_id"])
    return _registered[key]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)   # 64 unreserved characters
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ---------------- the connector row ----------------

def _connector(connector_id) -> dict:
    row = db.one("SELECT * FROM connectors WHERE id=?", (int(connector_id),))
    if not row:
        raise OAuthError(f"There is no connector {connector_id}.")
    return row


def _config(row: dict) -> dict:
    try:
        cfg = json.loads(row.get("config") or "{}")
    except ValueError:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _server_url(row: dict, cfg: dict) -> str:
    url = str(cfg.get("url") or "").strip()
    if not url:   # the address the connector framework connects to: the definition's mcp_url(inst) or mcp url
        try:
            from . import connectors
            url = connectors.mcp_url({**row, "config": cfg})
        except Exception:
            url = ""
    if not url:
        raise OAuthError("This connector has no server URL to sign in to.")
    return url


def _label(row: dict) -> str:
    try:
        from . import connectors
        label = (connectors.DEFINITIONS.get(row.get("key")) or {}).get("label")
    except Exception:
        label = None
    return str(label or row.get("key") or "the service")


# ---------------- the flow ----------------

def start(connector_id: int) -> str:
    """Begin a sign-in for a connector: returns the authorization URL to open in the browser."""
    row = _connector(connector_id)
    cfg = _config(row)
    resource = _server_url(row, cfg)
    _require_https(resource, "The server URL")
    found = _discover(resource)
    meta, issuer = found["meta"], found["issuer"]
    configured = str(cfg.get("oauth_client_id") or "").strip()
    if meta.get("registration_endpoint"):
        try:
            client_id = _register(meta, issuer)
        except OAuthError:
            if not configured:
                raise
            client_id = configured
    elif configured:
        client_id = configured
    else:
        raise OAuthError(f"{issuer} does not offer automatic registration. Create an OAuth app there with the "
                         f"redirect URL {redirect_uri()} and enter its client ID on the connector card.")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for old_state, p in list(_pending.items()):
            if now - p["created_at"] > PENDING_TTL:
                del _pending[old_state]
        _pending[state] = {"connector_id": int(row["id"]), "verifier": verifier,
                           "token_endpoint": meta["token_endpoint"], "issuer": issuer, "resource": resource,
                           "client_id": client_id, "created_at": now, "redirect_uri": redirect_uri(),
                           "iss_required": bool(meta.get("authorization_response_iss_parameter_supported"))}
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri(),
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": resource}
    if found["scope"]:
        params["scope"] = found["scope"]
    endpoint = meta["authorization_endpoint"]
    return endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(params)


def _expires_at(data: dict) -> float | None:
    try:
        return time.time() + float(data["expires_in"])
    except (KeyError, TypeError, ValueError):
        return None


def _token_request(endpoint: str, form: dict) -> dict:
    status, raw = _http(endpoint, urllib.parse.urlencode(form).encode("utf-8"), "application/x-www-form-urlencoded")
    data = _json(raw)
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(f"The token endpoint refused (HTTP {status}: {_error_text(data)}).",
                         needs_sign_in=status in (400, 401))
    kind = str(data.get("token_type") or "Bearer")
    if kind.lower() != "bearer":
        raise OAuthError(f"The token endpoint issued a {kind[:20]} token; Holdco only uses Bearer tokens.")
    return data


def _save(connector_id: int, token_set: dict) -> None:
    try:
        vault.put(f"oauth.{int(connector_id)}", json.dumps(token_set))
    except Exception as e:
        raise OAuthError(f"Could not save the sign-in in the vault: {e}") from None


def finish(query: dict) -> dict:
    """The callback: check state and iss, exchange the code, keep the tokens. Returns the connector row."""
    q = {str(k): (v[0] if isinstance(v, list) and v else v) for k, v in (query or {}).items()}
    with _lock:
        pending = _pending.pop(str(q.get("state") or ""), None)
    if q.get("error"):
        desc = str(q.get("error_description") or "")[:200]
        raise OAuthError(f"The sign-in was not completed: {str(q['error'])[:80]}" + (f" ({desc})." if desc else "."))
    if not pending or time.time() - pending["created_at"] > PENDING_TTL:
        raise OAuthError("This sign-in has expired or was already used. Press Sign in again on the connector card.")
    iss = q.get("iss")
    if iss is not None and not _same_issuer(str(iss), pending["issuer"]):
        raise OAuthError(f"The sign-in came back from {str(iss)[:200]}, not from {pending['issuer']} where it "
                         "started, so Holdco refused it and saved nothing.")
    if iss is None and pending.get("iss_required"):
        raise OAuthError(f"The sign-in came back without naming its issuer, which {pending['issuer']} promises "
                         "to do, so Holdco refused it and saved nothing.")
    if not q.get("code"):
        raise OAuthError("The sign-in came back without a code. Press Sign in again on the connector card.")
    data = _token_request(pending["token_endpoint"], {
        "grant_type": "authorization_code", "code": q["code"], "redirect_uri": pending["redirect_uri"],
        "client_id": pending["client_id"], "code_verifier": pending["verifier"], "resource": pending["resource"]})
    cid = pending["connector_id"]
    _save(cid, {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or "",
                "expires_at": _expires_at(data), "issuer": pending["issuer"], "resource": pending["resource"],
                "client_id": pending["client_id"], "token_endpoint": pending["token_endpoint"]})
    # An empty status tells the connector framework to check this instance's health again.
    db.run("UPDATE connectors SET status='', detail='' WHERE id=?", (cid,))
    try:   # the framework drops its cached client and tool list, so the next call signs in with the new token
        from . import connectors
        connectors.mark_stale(cid)
        company_id = (db.one("SELECT company_id FROM connectors WHERE id=?", (cid,)) or {}).get("company_id")
        connectors._changed(company_id)   # open dashboards reload the card ("Signed in")
    except Exception:
        pass
    return db.one("SELECT * FROM connectors WHERE id=?", (cid,)) or {"id": cid}


def _page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + html.escape(title) + "</title></head>"
            "<body style=\"font-family: system-ui, sans-serif; margin: 3em; line-height: 1.5\">"
            f"<h1 style=\"font-size: 1.3em\">{html.escape(title)}</h1>{body}</body></html>")


def callback_page(query: dict) -> tuple[int, str]:
    """GET /oauth/callback: (status, HTML)."""
    try:
        row = finish(query)
    except OAuthError as e:
        return 200, _page("Sign-in failed", str(e), "You can close this tab.")
    except Exception as e:   # the page must still say what happened, never a stack trace
        return 200, _page("Sign-in failed", f"Something went wrong: {type(e).__name__}: {e}", "You can close this tab.")
    return 200, _page("Signed in", f"Connected {_label(row)}. You can close this tab.")


# ---------------- using the tokens ----------------

class TokenSource:
    """Supplies the bearer token for one connector, refreshing it when fewer than 60 s remain.

    mcp.Client calls token(url) with the URL it is about to call; any origin other than the signed-in server's is
    refused, so a token never travels anywhere else.
    """

    def __init__(self, connector_id: int):
        self.connector_id = int(connector_id)

    def _load(self) -> dict | None:
        try:
            raw = vault.get(f"oauth.{self.connector_id}")
        except Exception as e:
            raise OAuthError(f"The saved sign-in cannot be read ({e}). Sign in again.", needs_sign_in=True) from None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("access_token") else None

    @property
    def resource(self) -> str | None:
        ts = self._load()
        return ts.get("resource") if ts else None

    @staticmethod
    def _expiring(ts: dict) -> bool:
        exp = ts.get("expires_at")
        return isinstance(exp, (int, float)) and exp - time.time() < REFRESH_MARGIN

    def token(self, url: str | None = None) -> str:
        ts = self._load()
        if ts is None:
            raise OAuthError("Not signed in yet: press Sign in on the connector card.", needs_sign_in=True)
        if url is not None and _origin(url) != _origin(ts.get("resource") or ""):
            raise OAuthError(f"Refusing to send this sign-in to {_origin(url)}: it belongs to "
                             f"{_origin(ts.get('resource') or '')}.")
        if self._expiring(ts):
            with _lock:
                lock = _refresh_locks.setdefault(self.connector_id, threading.Lock())
            with lock:   # one refresh at a time: a used refresh token may not work twice
                ts = self._load() or ts
                if self._expiring(ts):
                    ts = self._refresh(ts)
        return ts["access_token"]

    def _refresh(self, ts: dict) -> dict:
        still_valid = (ts.get("expires_at") or 0) > time.time()
        if not ts.get("refresh_token"):
            if still_valid:
                return ts
            raise OAuthError("The sign-in has expired. Press Sign in again on the connector card.", needs_sign_in=True)
        try:
            data = _token_request(ts["token_endpoint"], {
                "grant_type": "refresh_token", "refresh_token": ts["refresh_token"], "client_id": ts["client_id"],
                "resource": ts["resource"]})
        except OAuthError as e:
            if still_valid:
                return ts   # use what is left of the current token; the next call tries again
            raise OAuthError(f"The sign-in has expired and could not be renewed: {e} Press Sign in again.",
                             needs_sign_in=e.needs_sign_in) from None
        new = {**ts, "access_token": data["access_token"],
               "refresh_token": data.get("refresh_token") or ts["refresh_token"], "expires_at": _expires_at(data)}
        _save(self.connector_id, new)
        return new
