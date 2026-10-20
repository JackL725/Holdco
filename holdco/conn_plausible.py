"""The plausible and umami connectors (Phase 4, docs/phases/phase-4-scale.md section 3): web analytics, polled.

Two definitions share this module. Both are company connectors with the same hourly metrics, stored with source
plausible or umami: analytics.visitors, analytics.visits and analytics.conversions (goal completions), each over the
last 7 days (UTC dates) with the note "7d". Each adds two read tools, <key>__top_pages(days) and <key>__sources(days).
Page paths and referrers are typed by whoever visits the site (referrer spam is a known trick), so those two tools
count as outside content and reading them taints the session (Phase 3). Nothing is ever written.

Plausible: the Stats API v2, POST <base_url>/api/v2/query with the key as a Bearer header and a JSON query
{site_id, metrics, date_range, dimensions, order_by, pagination}, answering {"results": [{"metrics": [...],
"dimensions": [...]}]}. On Plausible's cloud the Stats API is a Business-plan feature; self-hosted has it.

Umami: GET <api>/websites/<website_id>/stats?startAt=&endAt= (milliseconds) answers pageviews, visitors and visits,
each a number or {"value": n, "prev": n} depending on the version (both are read); GET <api>/websites/<id>/metrics
?type=&startAt=&endAt=&limit= answers [{"x": name, "y": count}] for pages, referrers and custom events (the events
are counted as conversions). <api> is https://api.umami.is/v1 for Umami Cloud (the API key goes in x-umami-api-key)
and <base_url>/api for a self-hosted server (a Bearer token from the card, or one from POST <api>/auth/login with
the username and password).

The paths, parameters and answer shapes are named in the constants below; verify them against the vendor docs
before connecting.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30            # seconds for one API call
HEALTH_TIMEOUT = 20     # the framework's health budget
WINDOW_DAYS = 7         # the metrics' window
TOP_N = 10              # rows the read tools show
MAX_DAYS = 90

# Plausible (verify against the vendor docs: Stats API v2)
PLAUSIBLE_BASE = "https://plausible.io"
PLAUSIBLE_QUERY = "/api/v2/query"
PLAUSIBLE_PAGE = "event:page"
PLAUSIBLE_SOURCE = "visit:source"
PLAUSIBLE_GOAL = "event:goal"

# Umami (verify against the vendor docs: the website stats and metrics endpoints, authentication)
UMAMI_CLOUD_API = "https://api.umami.is/v1"
UMAMI_CLOUD_HOST = "api.umami.is"
UMAMI_KEY_HEADER = "x-umami-api-key"
UMAMI_LOGIN = "/auth/login"
UMAMI_PAGE_TYPES = ("url", "path")   # newer versions call the page metric "path"; the first one answering is used
UMAMI_SOURCE_TYPE = "referrer"
UMAMI_EVENT_TYPE = "event"

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # fakes and LAN servers skip any proxy
_proxied = urllib.request.build_opener()
_lock = threading.Lock()
_umami_tokens: dict[tuple, str] = {}   # (instance id, api, username) -> a login token


class AnalyticsError(RuntimeError):
    """An analytics call failed. `status` is the HTTP status (0 when the service could not be reached)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- helpers

def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _secrets(inst: dict | None) -> dict:
    if not inst:
        return {}
    from . import connectors   # imported here: connectors imports the conn_* modules
    return connectors.secrets(inst) or {}


def _days(days, default: int = WINDOW_DAYS) -> int:
    try:
        return max(1, min(int(days or default), MAX_DAYS))
    except (TypeError, ValueError):
        return default


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _num(v) -> float:
    """A count from either answer shape: a number, or {"value": n}."""
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def _local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def _scrub(text: str, secrets: list[str]) -> str:
    text = str(text or "")
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[key]")
    return text


def _http(method: str, url: str, headers: dict, body=None, secrets: list[str] | None = None,
          timeout: float = TIMEOUT, service: str = "The analytics service"):
    """One JSON call. Returns (status, parsed answer, text prefix); a network failure raises AnalyticsError."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", "User-Agent": "Holdco", **headers}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    opener = _direct if _local(url) else _proxied
    secrets = secrets or []
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(50_000)
    except urllib.error.URLError as e:
        raise AnalyticsError(_scrub(f"Cannot reach {service}: {e.reason}", secrets)) from None
    except (TimeoutError, socket.timeout):
        raise AnalyticsError(f"{service} did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise AnalyticsError(_scrub(f"Cannot reach {service}: {e}", secrets)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        parsed = {}
    return status, parsed, _scrub(text[:300], secrets)


def _error_of(parsed, text: str) -> str:
    if isinstance(parsed, dict):
        err = parsed.get("error") or parsed.get("message")
        if isinstance(err, dict):
            err = err.get("message")
        if err:
            return str(err)[:300]
    return text[:200]


# ---------------------------------------------------------------- Plausible

def _plausible_base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("base_url") or "").strip() or PLAUSIBLE_BASE).rstrip("/")


def _site(inst: dict | None) -> str:
    return str(_cfg(inst).get("site_id") or "").strip()


def _plausible(inst: dict, query: dict, timeout: float = TIMEOUT) -> list[dict]:
    """One Stats API v2 query for the card's site: the result rows."""
    key = str(_secrets(inst).get("api_key") or "").strip()
    if not key:
        raise AnalyticsError("No Plausible API key is set. Add one on the Plausible card in the company's Settings.")
    site = _site(inst)
    if not site:
        raise AnalyticsError("No site is set: put the site's domain (as Plausible shows it) on the Plausible card.")
    status, parsed, text = _http("POST", _plausible_base(inst) + PLAUSIBLE_QUERY, {"Authorization": f"Bearer {key}"},
                                 {"site_id": site, **query}, [key], timeout, "Plausible")
    if status in (401, 402, 403):
        raise AnalyticsError(f"Plausible refused the key (HTTP {status}: {_error_of(parsed, text)}). The Stats API "
                             "needs a Business plan on Plausible's cloud, or a self-hosted server.", status)
    if status >= 400:
        raise AnalyticsError(f"Plausible answered HTTP {status}: {_error_of(parsed, text)}", status)
    rows = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise AnalyticsError(f"Plausible answered without results: {text[:200]}", status)
    return [r for r in rows if isinstance(r, dict)]


def _range(days: int, end: _dt.date | None = None) -> list[str]:
    """Plausible's date_range for the last `days` UTC days, today included."""
    end = end or _today()
    return [(end - _dt.timedelta(days=days - 1)).isoformat(), end.isoformat()]


def _metric(rows: list[dict], i: int) -> float:
    if not rows:
        return 0.0
    m = rows[0].get("metrics") or []
    return _num(m[i]) if len(m) > i else 0.0


def plausible_health(inst: dict) -> tuple[str, str]:
    yesterday = _today() - _dt.timedelta(days=1)
    try:
        rows = _plausible(inst, {"metrics": ["visitors"], "date_range": [yesterday.isoformat()] * 2},
                          timeout=HEALTH_TIMEOUT)
    except AnalyticsError as e:
        return "bad", str(e)[:300]
    return "ok", f"{_site(inst)}: {_fmt(_metric(rows, 0))} visitors yesterday"


def plausible_metrics(inst: dict, company: dict | None) -> list[tuple[str, float, str]]:
    window = _range(WINDOW_DAYS)
    totals = _plausible(inst, {"metrics": ["visitors", "visits"], "date_range": window})
    goals = _plausible(inst, {"metrics": ["events"], "date_range": window, "dimensions": [PLAUSIBLE_GOAL]})
    conversions = sum(_num((r.get("metrics") or [0])[0]) for r in goals)
    return [("analytics.visitors", _metric(totals, 0), "7d"), ("analytics.visits", _metric(totals, 1), "7d"),
            ("analytics.conversions", conversions, "7d")]


def _plausible_top(inst: dict, dimension: str, metrics: list[str], days) -> list[dict]:
    return _plausible(inst, {"metrics": metrics, "date_range": _range(_days(days)), "dimensions": [dimension],
                             "order_by": [[metrics[0], "desc"]], "pagination": {"limit": TOP_N}})


def plausible_top_pages(inst: dict, company: dict | None, days: int = WINDOW_DAYS) -> str:
    days = _days(days)
    rows = _plausible_top(inst, PLAUSIBLE_PAGE, ["visitors", "pageviews"], days)
    if not rows:
        return f"No page views on {_site(inst)} in the last {days} day(s) (Plausible)."
    lines = [f"Top pages on {_site(inst)}, last {days} day(s) (Plausible):"]
    for r in rows[:TOP_N]:
        dims, m = r.get("dimensions") or ["?"], r.get("metrics") or []
        lines.append(f"- {dims[0]}: {_fmt(_num(m[0] if m else 0))} visitors, "
                     f"{_fmt(_num(m[1] if len(m) > 1 else 0))} pageviews")
    return "\n".join(lines)


def plausible_sources(inst: dict, company: dict | None, days: int = WINDOW_DAYS) -> str:
    days = _days(days)
    rows = _plausible_top(inst, PLAUSIBLE_SOURCE, ["visitors"], days)
    if not rows:
        return f"No visits to {_site(inst)} in the last {days} day(s) (Plausible)."
    lines = [f"Where visitors to {_site(inst)} came from, last {days} day(s) (Plausible):"]
    for r in rows[:TOP_N]:
        dims, m = r.get("dimensions") or ["?"], r.get("metrics") or []
        lines.append(f"- {dims[0] or 'Direct / None'}: {_fmt(_num(m[0] if m else 0))} visitors")
    return "\n".join(lines)


# ---------------------------------------------------------------- Umami

def _umami_api(inst: dict | None) -> str:
    """The API root: Umami Cloud's /v1, or a self-hosted server's /api."""
    base = (str(_cfg(inst).get("base_url") or "").strip() or UMAMI_CLOUD_API).rstrip("/")
    if (urllib.parse.urlparse(base).hostname or "").lower() == UMAMI_CLOUD_HOST:
        return base if base.endswith("/v1") else base + "/v1"
    return base if base.endswith("/api") else base + "/api"


def _website(inst: dict | None) -> str:
    return str(_cfg(inst).get("website_id") or "").strip()


def _cloud(inst: dict | None) -> bool:
    return (urllib.parse.urlparse(_umami_api(inst)).hostname or "").lower() == UMAMI_CLOUD_HOST


def _login(inst: dict, fresh: bool = False) -> str:
    """A token from POST <api>/auth/login with the card's username and password, kept in memory."""
    s = _secrets(inst)
    user, password = str(_cfg(inst).get("username") or "").strip(), str(s.get("password") or "")
    api = _umami_api(inst)
    cache_key = (inst.get("id"), api, user)
    with _lock:
        token = None if fresh else _umami_tokens.get(cache_key)
    if token:
        return token
    status, parsed, text = _http("POST", api + UMAMI_LOGIN, {}, {"username": user, "password": password},
                                 [password], HEALTH_TIMEOUT, "Umami")
    token = str((parsed or {}).get("token") or "") if isinstance(parsed, dict) else ""
    if status >= 400 or not token:
        raise AnalyticsError(f"Umami refused the username and password (HTTP {status}: {_error_of(parsed, text)})",
                             status)
    with _lock:
        _umami_tokens[cache_key] = token
    return token


def _umami_auth(inst: dict, fresh: bool = False) -> tuple[dict, list[str]]:
    """(headers, secrets to scrub) for one Umami call."""
    s = _secrets(inst)
    token = str(s.get("api_token") or "").strip()
    if token:
        header = {UMAMI_KEY_HEADER: token} if _cloud(inst) else {"Authorization": f"Bearer {token}"}
        return header, [token]
    if str(_cfg(inst).get("username") or "").strip() and s.get("password"):
        login = _login(inst, fresh)
        return {"Authorization": f"Bearer {login}"}, [login, str(s.get("password"))]
    raise AnalyticsError("No Umami API token, or username and password, is set. Add them on the Umami card.")


def _umami(inst: dict, path: str, params: dict, timeout: float = TIMEOUT):
    """One GET under the card's website, logging in again once when a saved login token has expired."""
    site = _website(inst)
    if not site:
        raise AnalyticsError("No website id is set: copy it from the website's settings in Umami.")
    url = f"{_umami_api(inst)}/websites/{urllib.parse.quote(site, safe='')}{path}?{urllib.parse.urlencode(params)}"
    for attempt in (0, 1):
        headers, secrets = _umami_auth(inst, fresh=attempt == 1)
        status, parsed, text = _http("GET", url, headers, None, secrets, timeout, "Umami")
        if status == 401 and attempt == 0 and not str(_secrets(inst).get("api_token") or "").strip():
            continue   # a login token expired: log in again once
        if status in (401, 403):
            raise AnalyticsError(f"Umami refused the credentials (HTTP {status}: {_error_of(parsed, text)})", status)
        if status >= 400:
            raise AnalyticsError(f"Umami answered HTTP {status}: {_error_of(parsed, text)}", status)
        return parsed
    raise AnalyticsError("Umami refused the credentials.", 401)


def _window_ms(days: int, end: _dt.datetime | None = None) -> dict:
    end = end or _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(days=days)
    return {"startAt": int(start.timestamp() * 1000), "endAt": int(end.timestamp() * 1000)}


def _umami_rows(inst: dict, kind, days: int, limit: int | None = TOP_N) -> list[dict]:
    """[{"x", "y"}] from the metrics endpoint for a type (or the first of several types that answers)."""
    kinds = list(kind) if isinstance(kind, (list, tuple)) else [kind]
    last: AnalyticsError | None = None
    for k in kinds:
        params = {"type": k, **_window_ms(days)}
        if limit:
            params["limit"] = limit
        try:
            rows = _umami(inst, "/metrics", params)
        except AnalyticsError as e:
            if e.status == 400 and k != kinds[-1]:   # an older or newer name for the same metric
                last = e
                continue
            raise
        return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    raise last or AnalyticsError("Umami did not answer.")


def umami_health(inst: dict) -> tuple[str, str]:
    today = _dt.datetime.now(_dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        stats = _umami(inst, "/stats", _window_ms(1, today), timeout=HEALTH_TIMEOUT)
    except AnalyticsError as e:
        return "bad", str(e)[:300]
    if not isinstance(stats, dict) or "visitors" not in stats:
        return "bad", "Umami answered without visitor numbers (is the website id right?)"
    return "ok", f"website {_website(inst)}: {_fmt(_num(stats.get('visitors')))} visitors yesterday"


def umami_metrics(inst: dict, company: dict | None) -> list[tuple[str, float, str]]:
    stats = _umami(inst, "/stats", _window_ms(WINDOW_DAYS))
    if not isinstance(stats, dict):
        raise AnalyticsError("Umami answered without numbers.")
    events = _umami_rows(inst, UMAMI_EVENT_TYPE, WINDOW_DAYS, limit=None)
    return [("analytics.visitors", _num(stats.get("visitors")), "7d"),
            ("analytics.visits", _num(stats.get("visits")), "7d"),
            ("analytics.conversions", sum(_num(r.get("y")) for r in events), "7d")]


def umami_top_pages(inst: dict, company: dict | None, days: int = WINDOW_DAYS) -> str:
    days = _days(days)
    rows = _umami_rows(inst, UMAMI_PAGE_TYPES, days)
    if not rows:
        return f"No page views in the last {days} day(s) (Umami)."
    return "\n".join([f"Top pages, last {days} day(s) (Umami):"]
                     + [f"- {r.get('x') or '?'}: {_fmt(_num(r.get('y')))} views" for r in rows[:TOP_N]])


def umami_sources(inst: dict, company: dict | None, days: int = WINDOW_DAYS) -> str:
    days = _days(days)
    rows = _umami_rows(inst, UMAMI_SOURCE_TYPE, days)
    if not rows:
        return f"No referred visits in the last {days} day(s) (Umami); direct visits are not listed."
    return "\n".join([f"Where visitors came from, last {days} day(s) (Umami):"]
                     + [f"- {r.get('x') or 'Direct'}: {_fmt(_num(r.get('y')))} visitors" for r in rows[:TOP_N]])


# ---------------------------------------------------------------- definitions

_DAYS = {"type": "object", "properties": {"days": {"type": "integer",
                                                    "description": f"How many days back (default 7, at most {MAX_DAYS})"}},
         "required": []}


def _tools(top_pages, sources, label: str) -> list[dict]:
    return [
        {"name": "top_pages", "tag": "read", "fn": top_pages, "external": True, "parameters": _DAYS,
         "description": f"The most visited pages of the company's site in the last N days, from {label} analytics."},
        {"name": "sources", "tag": "read", "fn": sources, "external": True, "parameters": _DAYS,
         "description": f"Where the site's visitors came from (referrers, search, social) in the last N days, from "
                        f"{label} analytics."},
    ]


AGENTS_CAN = ["Read visitors, visits and goal conversions (polled hourly into the brain's metrics)",
              "Read the top pages and traffic sources for the weekly review and SEO work",
              "Nothing is ever written"]

PLAUSIBLE = {
    "label": "Plausible",
    "category": "Analytics",
    "kind": "api",
    "scope": "company",
    "setup": {
        "where_to_get_key": ("In Plausible: Account settings > API keys > New API key, choose the Stats API. On "
                             "Plausible's cloud the Stats API is a Business-plan feature; a self-hosted server has "
                             "it (set its address below)."),
        "permissions": ["Stats API (read only)"],
        "fields": [
            {"key": "api_key", "label": "Stats API key", "secret": True, "hint": "Read only", "placeholder": ""},
            {"key": "site_id", "label": "Site", "secret": False,
             "hint": "The site's domain exactly as Plausible lists it", "placeholder": "pawplan.example"},
            {"key": "base_url", "label": "Server address", "secret": False,
             "hint": "Leave blank for Plausible's cloud; your server's address when self-hosted",
             "placeholder": PLAUSIBLE_BASE},
        ],
        "test_mode": "Not applicable: it only reads.",
        "docs_url": "https://plausible.io/docs/stats-api",
        "agents_can": AGENTS_CAN,
    },
    "health": plausible_health,
    "health_timeout": HEALTH_TIMEOUT + 5,
    "metrics": plausible_metrics,
    "tools": lambda inst: _tools(plausible_top_pages, plausible_sources, "Plausible"),
}

UMAMI = {
    "label": "Umami",
    "category": "Analytics",
    "kind": "api",
    "scope": "company",
    "setup": {
        "where_to_get_key": ("Umami Cloud: Settings > API keys > Create key, and leave the server address blank. "
                             "Self-hosted: put your server's address and either an API token or the username and "
                             "password of a view-only user. The website id is in the website's settings."),
        "permissions": ["View access to the website"],
        "fields": [
            {"key": "base_url", "label": "Server address", "secret": False,
             "hint": "Blank for Umami Cloud; your server's address when self-hosted", "placeholder": UMAMI_CLOUD_API},
            {"key": "api_token", "label": "API key or token", "secret": True,
             "hint": "Umami Cloud's API key, or a self-hosted token", "placeholder": ""},
            {"key": "username", "label": "Username (self-hosted)", "secret": False,
             "hint": "Instead of a token: a user who can view the website", "placeholder": ""},
            {"key": "password", "label": "Password (self-hosted)", "secret": True, "hint": "", "placeholder": ""},
            {"key": "website_id", "label": "Website id", "secret": False,
             "hint": "From the website's settings in Umami", "placeholder": "a1b2c3d4-..."},
        ],
        "test_mode": "Not applicable: it only reads.",
        "docs_url": "https://umami.is/docs/api",
        "agents_can": AGENTS_CAN,
    },
    "health": umami_health,
    "health_timeout": HEALTH_TIMEOUT * 2 + 5,   # a login, then the stats query
    "metrics": umami_metrics,
    "tools": lambda inst: _tools(umami_top_pages, umami_sources, "Umami"),
}

# Both definitions this module provides, by key (connectors.DEFINITIONS merges them).
DEFINITIONS = {"plausible": PLAUSIBLE, "umami": UMAMI}
