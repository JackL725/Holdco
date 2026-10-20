"""The cloudflare connector (Phase 4, docs/phases/phase-4-scale.md section 3): Workers static assets as a second
deploy target beside Netlify.

actions.execute("deploy_site") asks deploy_if_selected(company) first. The company setting site_provider picks the
target: netlify (never Cloudflare), cloudflare (Cloudflare, or a clear error when it is not set up), auto (Cloudflare
when the company has a healthy Cloudflare card, else Netlify as before). So a Cloudflare deploy is still a
deploy_site request: its policy, the trust ladder and the taint rules apply exactly as to a Netlify deploy.

The deploy, in the API's words (https://api.cloudflare.com/client/v4, Bearer API token):
1. start an assets upload session for the Worker with a manifest of every file in site/ (path -> hash and size);
   Cloudflare answers which files it does not have yet, in buckets, and an upload token;
2. upload those files, bucket by bucket, with the upload token (base64 parts named by their hash); the last upload
   answers the completion token (a session with nothing to upload answers it at once);
3. create or update the Worker with that token in its metadata and an assets binding (a tiny module that hands
   every request to the assets);
4. turn on its workers.dev address, and attach the custom domain when the card has one (found in the zone it
   belongs to). site_url becomes the custom domain, else the workers.dev address, like the Netlify path stores its.

The only agent tool is cloudflare__deploy_status (read): the Worker's last deployment and its address.

The endpoint paths are the PATH_* constants below, taken from Cloudflare's API reference for Workers static assets
(direct upload), Worker scripts, subdomains, deployments, zones and Workers custom domains. Verify them against
Cloudflare's API documentation before connecting.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT = 30             # seconds for one API call
UPLOAD_TIMEOUT = 120     # one bucket of files, like the Netlify upload
HEALTH_TIMEOUT = 15
COMPATIBILITY_DATE = "2025-09-01"
MAX_FILE_BYTES = 25 * 1024 * 1024   # Workers static assets: 25 MiB per file
MAX_FILES = 20000                   # and 20,000 files per Worker version
MODULE_NAME = "holdco-site.js"
# The Worker itself: every request that is not a file goes to the assets binding (404 page, trailing slashes).
MODULE = "export default { async fetch(request, env) { return env.ASSETS.fetch(request); } };\n"
ASSETS_CONFIG = {"html_handling": "auto-trailing-slash", "not_found_handling": "404-page"}
PROVIDERS = ("auto", "netlify", "cloudflare")

# Endpoint paths (verify against Cloudflare's API documentation before connecting).
PATH_VERIFY = "/user/tokens/verify"                                         # user API tokens
PATH_ACCOUNT_VERIFY = "/accounts/{account}/tokens/verify"                   # account-owned API tokens
PATH_SUBDOMAIN = "/accounts/{account}/workers/subdomain"                    # the account's <name>.workers.dev
PATH_SESSION = "/accounts/{account}/workers/scripts/{script}/assets-upload-session"
PATH_UPLOAD = "/accounts/{account}/workers/assets/upload?base64=true"
PATH_SCRIPT = "/accounts/{account}/workers/scripts/{script}"
PATH_SCRIPT_SUBDOMAIN = "/accounts/{account}/workers/scripts/{script}/subdomain"
PATH_DEPLOYMENTS = "/accounts/{account}/workers/scripts/{script}/deployments"
PATH_ZONES = "/zones"
PATH_DOMAINS = "/accounts/{account}/workers/domains"

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # fakes on 127.0.0.1 skip any proxy
_proxied = urllib.request.build_opener()


class CloudflareError(RuntimeError):
    """A Cloudflare call failed. `status` is the HTTP status (0 when Cloudflare could not be reached)."""

    def __init__(self, message: str, status: int = 0, codes: tuple = ()):
        super().__init__(message)
        self.status = status
        self.codes = codes


# ---------------------------------------------------------------- instance helpers

def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _token(inst: dict | None) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get("api_token") or "").strip()


def _base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or API_BASE).rstrip("/")


def _account(inst: dict | None) -> str:
    return str(_cfg(inst).get("account_id") or "").strip()


def custom_domain(inst: dict | None) -> str:
    d = str(_cfg(inst).get("custom_domain") or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/", 1)[0].rstrip(".")
    return d


def worker_name(inst: dict | None, company: dict | None) -> str:
    """The card's worker_name, else <slug>-site; Worker names are lower-case letters, digits and dashes (63 at most)."""
    raw = str(_cfg(inst).get("worker_name") or "").strip() or f"{(company or {}).get('slug') or 'holdco'}-site"
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw.lower())).strip("-")[:63].strip("-")
    return name or "holdco-site"


def _scrub(text: str, token: str = "") -> str:
    text = str(text or "")
    if token and len(token) >= 6:
        text = text.replace(token, "[token]")
    return text


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------- HTTP

def _request(inst: dict, method: str, path: str, body=None, *, data: bytes | None = None,
             content_type: str = "", bearer: str = "", timeout: float = TIMEOUT):
    """One call. JSON body (or raw data with its content type); bearer replaces the API token (the upload token).
    Returns Cloudflare's result; an HTTP error or success: false raises CloudflareError."""
    token = _token(inst)
    auth = bearer or token
    if not auth:
        raise CloudflareError("No Cloudflare API token is set. Add one on the Cloudflare card in the company's "
                              "Settings.")
    url = _base(inst) + path
    headers = {"Authorization": f"Bearer {auth}", "User-Agent": "Holdco", "Accept": "application/json"}
    if body is not None:
        data, content_type = json.dumps(body).encode(), "application/json"
    if data is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(5_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(200_000)
    except urllib.error.URLError as e:
        raise CloudflareError(_scrub(f"Cannot reach Cloudflare: {e.reason}", auth)) from None
    except (TimeoutError, socket.timeout):
        raise CloudflareError("Cloudflare did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise CloudflareError(_scrub(f"Cannot reach Cloudflare: {e}", auth)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text or "{}")
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    if status >= 400 or parsed.get("success") is False:
        errors = [e for e in (parsed.get("errors") or []) if isinstance(e, dict)]
        msg = "; ".join(str(e.get("message") or "") for e in errors if e.get("message")) or text[:200]
        codes = tuple(e.get("code") for e in errors)
        raise CloudflareError(_scrub(f"Cloudflare answered HTTP {status}: {msg}", auth), status, codes)
    return parsed.get("result")


def _multipart(parts: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """multipart/form-data from (name, filename, content type, bytes) parts; an empty filename sends none."""
    boundary = f"holdco{secrets.token_hex(12)}"
    out = bytearray()
    for name, filename, ctype, payload in parts:
        out += f"--{boundary}\r\n".encode()
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"Content-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
        out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- the deploy

def _files(site_dir) -> list[dict]:
    """Every file under site/ as {path: "/a/b.html", data, hash, size, type}. A link is never followed (code in the
    sandbox can make one pointing anywhere on this PC), as in the Netlify path."""
    out = []
    for f in sorted(site_dir.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(site_dir).as_posix()
        data = f.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise RuntimeError(f"site/{rel} is larger than 25 MB, the most Cloudflare serves as one file.")
        ext = f.suffix.lstrip(".").lower()
        # The hash names the content for Cloudflare: 32 hex characters, from the content and the extension.
        digest = hashlib.sha256(base64.b64encode(data) + ext.encode()).hexdigest()[:32]
        out.append({"path": "/" + rel, "data": data, "hash": digest, "size": len(data),
                    "type": mimetypes.guess_type(f.name)[0] or "application/octet-stream"})
    if len(out) > MAX_FILES:
        raise RuntimeError(f"site/ has {len(out)} files; Cloudflare takes at most {MAX_FILES} per Worker.")
    return out


def _subdomain(inst: dict) -> str:
    """The account's workers.dev subdomain ("" when it has none yet)."""
    try:
        result = _request(inst, "GET", PATH_SUBDOMAIN.format(account=urllib.parse.quote(_account(inst), safe="")))
    except CloudflareError as e:
        if e.status == 404:
            return ""
        raise
    return str((result or {}).get("subdomain") or "").strip()


def _zone_id(inst: dict, hostname: str) -> str:
    """The id of the zone the hostname belongs to: the longest suffix Cloudflare knows (www.pawplan.co.uk finds
    pawplan.co.uk)."""
    labels = hostname.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        found = _request(inst, "GET", f"{PATH_ZONES}?{urllib.parse.urlencode({'name': name})}") or []
        if isinstance(found, list) and found and isinstance(found[0], dict) and found[0].get("id"):
            return str(found[0]["id"])
    raise RuntimeError(f"{hostname} is not in a zone of this Cloudflare account: add the domain to Cloudflare "
                       "first, or clear the custom domain on the card.")


def deploy(inst: dict, company: dict) -> str:
    """Upload site/ as the Worker's static assets and publish it. Returns the result line; raises RuntimeError."""
    from . import db, workspace   # imported here: workspace and db are only needed for a deploy
    site_dir = workspace.safe_path(company, "site")
    if not (site_dir / "index.html").exists():
        raise RuntimeError("site/index.html doesn't exist yet.")
    account = _account(inst)
    if not _token(inst) or not account:
        raise RuntimeError("The Cloudflare card needs an API token and the account id before it can deploy.")
    script = worker_name(inst, company)
    acct = urllib.parse.quote(account, safe="")
    files = _files(site_dir)
    try:
        manifest = {f["path"]: {"hash": f["hash"], "size": f["size"]} for f in files}
        session = _request(inst, "POST", PATH_SESSION.format(account=acct, script=script), {"manifest": manifest}) or {}
        jwt = str(session.get("jwt") or "")
        buckets = [b for b in (session.get("buckets") or []) if b]
        by_hash = {f["hash"]: f for f in files}
        uploaded = 0
        for bucket in buckets:
            parts = []
            for h in bucket:
                f = by_hash.get(h)
                if f is None:
                    raise RuntimeError("Cloudflare asked for a file Holdco did not offer; deploy again.")
                parts.append((h, h, f["type"], base64.b64encode(f["data"])))
            data, ctype = _multipart(parts)
            result = _request(inst, "POST", PATH_UPLOAD.format(account=acct), data=data, content_type=ctype,
                              bearer=jwt, timeout=UPLOAD_TIMEOUT) or {}
            uploaded += len(bucket)
            if result.get("jwt"):
                jwt = str(result["jwt"])   # the last bucket answers the completion token
        if not jwt:
            raise RuntimeError("Cloudflare did not hand out an upload token; deploy again.")
        metadata = {"main_module": MODULE_NAME, "compatibility_date": COMPATIBILITY_DATE,
                    "assets": {"jwt": jwt, "config": ASSETS_CONFIG},
                    "bindings": [{"type": "assets", "name": "ASSETS"}]}
        data, ctype = _multipart([("metadata", "", "application/json", json.dumps(metadata).encode()),
                                  (MODULE_NAME, MODULE_NAME, "application/javascript+module", MODULE.encode())])
        _request(inst, "PUT", PATH_SCRIPT.format(account=acct, script=script), data=data, content_type=ctype,
                 timeout=UPLOAD_TIMEOUT)
        _request(inst, "POST", PATH_SCRIPT_SUBDOMAIN.format(account=acct, script=script), {"enabled": True})
        sub = _subdomain(inst)
        url = f"https://{script}.{sub}.workers.dev" if sub else ""
        host = custom_domain(inst)
        if host:
            _request(inst, "PUT", PATH_DOMAINS.format(account=acct),
                     {"environment": "production", "hostname": host, "service": script,
                      "zone_id": _zone_id(inst, host)})
            url = f"https://{host}"
    except CloudflareError as e:
        raise RuntimeError(f"Cloudflare deploy failed: {e}") from None
    if url:
        db.update_company_settings(company["id"], site_url=url)   # only this key: never the resolved secrets
        where = url
    else:
        where = (f"the Worker {script} (no workers.dev subdomain on the account yet: pick one in Cloudflare's "
                 "Workers settings, or set a custom domain on the card)")
    return (f"Website deployed to Cloudflare Workers: {where} ({len(files)} file{'s' if len(files) != 1 else ''}, "
            f"{uploaded} uploaded)")


def provider(company: dict) -> str:
    """The company's site_provider (auto, netlify or cloudflare; anything else counts as auto)."""
    from . import db
    v = str(db.company_settings(company).get("site_provider") or "auto").strip().lower()
    return v if v in PROVIDERS else "auto"


def deploy_if_selected(company: dict) -> str | None:
    """actions.execute("deploy_site"): the Cloudflare deploy result when the company's site_provider picks
    Cloudflare (cloudflare, or auto with a healthy Cloudflare card), else None for the Netlify path. A Cloudflare
    failure is a failure (the approval shows it and can be retried), never a quiet fall back to Netlify."""
    choice = provider(company)
    if choice == "netlify":
        return None
    from . import connectors   # imported here: connectors imports the conn_* modules
    inst = connectors.instance("cloudflare", company)
    usable = bool(inst and inst.get("enabled") and inst.get("company_id") == company.get("id"))
    if choice == "cloudflare":
        if not usable:
            raise RuntimeError("The site provider is Cloudflare, but this company has no Cloudflare card turned on. "
                               "Add it in the company's Settings (Connectors), or set the site provider to netlify "
                               "or auto, then retry this request.")
        return deploy(inst, company)
    if usable and inst.get("status") == "ok":
        return deploy(inst, company)
    return None


# ---------------------------------------------------------------- the tool

def _when(value) -> str:
    s = str(value or "")
    try:
        t = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s or "?"
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def deploy_status(inst: dict, company: dict | None) -> str:
    if not company:
        return "ERROR: no company to report on."
    from . import db
    script = worker_name(inst, company)
    acct = urllib.parse.quote(_account(inst), safe="")
    try:
        result = _request(inst, "GET", PATH_DEPLOYMENTS.format(account=acct, script=script)) or {}
    except CloudflareError as e:
        if e.status == 404:
            return f"Nothing deployed to Cloudflare yet: the Worker {script} does not exist."
        raise
    deps = [d for d in (result.get("deployments") or []) if isinstance(d, dict)]
    if not deps:
        return f"Nothing deployed to Cloudflare yet: the Worker {script} has no deployment."
    last = max(deps, key=lambda d: str(d.get("created_on") or ""))
    url = str(db.company_settings(company).get("site_url") or "")
    host = custom_domain(inst)
    if host:
        url = f"https://{host}"
    elif ".workers.dev" not in url:
        sub = _subdomain(inst)
        url = f"https://{script}.{sub}.workers.dev" if sub else url
    return (f"Last Cloudflare deploy of the Worker {script}: {_when(last.get('created_on'))} (deployment "
            f"{last.get('id') or '?'}). Address: {url or 'none yet (no workers.dev subdomain and no custom domain)'}. "
            f"Site provider: {provider(company)}.")


TOOLS = [
    {"name": "deploy_status", "tag": "read", "fn": deploy_status,
     "description": "When the website was last deployed to Cloudflare Workers, and its address. To publish the "
                    "site, use deploy_site.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


# ---------------------------------------------------------------- health

def health(inst: dict) -> tuple[str, str]:
    if not _token(inst):
        return "bad", "No token: paste an API token limited to Workers"
    try:
        result = _request(inst, "GET", PATH_VERIFY, timeout=HEALTH_TIMEOUT) or {}
    except CloudflareError as e:
        if e.status in (400, 401, 403) and _account(inst):
            try:   # an account-owned token is verified on the account
                result = _request(inst, "GET", PATH_ACCOUNT_VERIFY.format(
                    account=urllib.parse.quote(_account(inst), safe="")), timeout=HEALTH_TIMEOUT) or {}
            except CloudflareError:
                return "bad", "token rejected"
        elif e.status in (400, 401, 403):
            return "bad", "token rejected"
        else:
            return "bad", str(e)[:300]
    state = str(result.get("status") or "active").lower()
    if state != "active":
        return "bad", f"the token is {state}"
    if not _account(inst):
        return "warn", "token active; add the account id (Workers & Pages overview, right-hand side)"
    try:
        sub = _subdomain(inst)
    except CloudflareError as e:
        if e.status in (401, 403):
            return "bad", "the token may not use Workers on this account (give it Workers Scripts: Edit)"
        return "bad", str(e)[:300]
    host = custom_domain(inst)
    if host:
        return "ok", f"token active, deploys go to {host}"
    if not sub:
        return "warn", ("token active, but the account has no workers.dev subdomain yet: pick one in Cloudflare's "
                        "Workers settings, or set a custom domain")
    return "ok", f"token active, deploys go to <worker>.{sub}.workers.dev"


DEFINITION = {
    "label": "Cloudflare",
    "category": "Hosting",
    "kind": "api",
    "scope": "company",
    "setup": {
        "where_to_get_key": (
            "In the Cloudflare dashboard open My Profile > API Tokens > Create Token and start from the Edit "
            "Cloudflare Workers template, limited to your account (and, for a custom domain, to its zone). Copy "
            "the account id from the Workers & Pages overview. Then set the company's site provider to cloudflare, "
            "or leave it on auto to deploy here whenever this card is healthy. Workers static assets are the "
            "recommended target over Pages."),
        "permissions": ["Account: Workers Scripts: Edit",
                        "Zone: Workers Routes: Edit and Zone: Read (only for a custom domain)"],
        "fields": [
            {"key": "api_token", "label": "API token", "secret": True,
             "hint": "A token limited to Workers on this account", "placeholder": "Cloudflare API token"},
            {"key": "account_id", "label": "Account id", "secret": False,
             "hint": "Workers & Pages overview, right-hand side", "placeholder": "0123456789abcdef0123456789abcdef"},
            {"key": "worker_name", "label": "Worker name", "secret": False,
             "hint": "Blank uses <company slug>-site", "placeholder": "pawplan-site"},
            {"key": "custom_domain", "label": "Custom domain (optional)", "secret": False,
             "hint": "A host name in a zone of this account, e.g. pawplan.co or www.pawplan.co",
             "placeholder": "pawplan.co"},
            {"key": "api_base", "label": "API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": API_BASE},
        ],
        "test_mode": ("Cloudflare has no test mode: a deploy is a deploy. Deploys keep asking you (the deploy_site "
                      "policy) until the trust ladder promotes them."),
        "docs_url": "https://developers.cloudflare.com/workers/static-assets/direct-upload/",
        "agents_can": ["Publish the site from site/ when you approve a deploy (deploy_site), here instead of Netlify",
                       "See when the site was last deployed and its address"],
    },
    "health": health,
    "tools": tools,
}

DEFINITIONS = {"cloudflare": DEFINITION}
