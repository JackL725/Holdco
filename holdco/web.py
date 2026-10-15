"""Web search and page fetching, standard library only.

Search runs through a chain of providers: the Brave Search API (needs a key), a SearXNG instance you run
yourself (needs its url), the optional `ddgs` package and DuckDuckGo's HTML pages (no key). The configured
provider goes first. A provider that fails or gets blocked is skipped for ten minutes while the others carry on,
and PROVIDER_STATUS tells the Doctor how each one is doing.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_BYTES = 3_000_000


class WebError(Exception):
    pass


# ---------------- safety ----------------

# Relay services fetch another site on an agent's behalf (a test company read a 403 page through r.jina.ai). They
# hide where requests really go and break the sites' terms, so they are refused. Exact hosts (and their
# subdomains), host suffixes, and host prefixes, kept in one place.
RELAY_HOSTS = ("r.jina.ai", "s.jina.ai", "12ft.io", "allorigins.win", "corsproxy.io", "thingproxy.freeboard.io")
RELAY_SUFFIXES = (".translate.goog",)
RELAY_PREFIXES = ("cors-anywhere",)
RELAY_HINT = "Use the search result's snippet, or find another source for the same information."
BLOCKED_HINT = ("The site refuses automated readers. Use the search result's snippet or another source; do not use "
                "relay services such as r.jina.ai (Holdco blocks them).")


def is_relay(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return (any(h == r or h.endswith("." + r) for r in RELAY_HOSTS) or h.endswith(RELAY_SUFFIXES)
            or h.startswith(RELAY_PREFIXES))


def check_public_url(url: str) -> str:
    """Agents may only reach the public internet, never this PC or your home network, and never through a relay."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError("Only http(s) URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise WebError("URL has no host.")
    if is_relay(host):
        raise WebError(f"{host} is a relay that fetches other sites for you. Relays hide where requests go and break "
                       f"the site's terms, so Holdco blocks them. {RELAY_HINT}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise WebError(f"Couldn't resolve {host}.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise WebError("Local and private network addresses are blocked for agents.")
    return url


# ---------------- HTML to text ----------------

class _Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "footer", "ul", "ol", "table", "blockquote", "pre", "dd", "dt"}

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []
        self.base = base_url

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("## ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._href = urllib.parse.urljoin(self.base, href)
                self._link_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            text = " ".join("".join(self._link_text).split())
            if text:
                self.links.append((text[:80], self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out, blank = [], False
        for line in lines:
            if not line:
                if not blank:
                    out.append("")
                blank = True
            else:
                out.append(line)
                blank = False
        return "\n".join(out).strip()


def html_to_text(html: str, base_url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    p = _Extractor(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed HTML: keep what we got
        pass
    return " ".join(p.title.split()), p.text(), p.links


# ---------------- fetching ----------------

def http_get(url: str, headers: dict | None = None, timeout: float = 25) -> tuple[int, str, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES)
            return resp.status, resp.headers.get("Content-Type", ""), body, resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read(200_000), url
    except urllib.error.URLError as e:
        raise WebError(f"Couldn't fetch {url}: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise WebError(f"Timed out fetching {url}") from None
    except (ConnectionError, OSError, ValueError) as e:
        raise WebError(f"Couldn't fetch {url}: {e}") from None


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    enc = m.group(1) if m else "utf-8"
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def fetch_page(url: str, max_chars: int = 9000, include_links: bool = False) -> str:
    check_public_url(url)
    status, ctype, body, final = http_get(url)
    text = _decode(body, ctype)
    if status >= 400:
        hint = f"{BLOCKED_HINT}\n" if status == 403 else ""
        return f"HTTP {status} fetching {url}\n{hint}{text[:500]}"
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, content, links = html_to_text(text, final)
        out = f"Title: {title}\nURL: {final}\n\n{content[:max_chars]}"
        if len(content) > max_chars:
            out += f"\n\n[truncated: page has {len(content)} characters]"
        if include_links and links:
            uniq, seen = [], set()
            for t, h in links:
                if h not in seen:
                    seen.add(h)
                    uniq.append(f"- {t}: {h}")
            out += "\n\nLinks:\n" + "\n".join(uniq[:40])
        return out
    return f"URL: {final}\nContent-Type: {ctype}\n\n{text[:max_chars]}"


# ---------------- search ----------------

class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._mode: str | None = None
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "a" and ("result__a" in cls or "result-link" in cls):
            href = a.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
            if href.startswith("//"):
                href = "https:" + href
            self._cur = {"title": "", "url": href, "snippet": ""}
            self.results.append(self._cur)
            self._mode = "title"
        elif ("result__snippet" in cls or "result-snippet" in cls) and self.results:
            self._cur = self.results[-1]
            self._mode = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "td", "div") and self._mode:
            self._mode = None

    def handle_data(self, data):
        if self._mode and self._cur is not None:
            self._cur[self._mode] += data


class ProviderError(WebError):
    """One search provider could not answer (blocked, down or misconfigured). The chain moves on to the next."""


PROVIDERS = ("brave", "searxng", "ddgs", "ddg_html")
COOLDOWN_SECONDS = 600          # a provider that failed or was blocked is left alone this long before a retry
ALERT_INTERVAL = 1800           # the "search is down" event is raised at most once per company this often
# DuckDuckGo answers 202 (with a challenge page), 403 or 429 when it decides a client is a bot.
BLOCK_STATUSES = (202, 403, 429)
# Phrases from DuckDuckGo's bot-challenge pages (the "anomaly" interstitial says "bots use DuckDuckGo too", the
# newer one is a "challenge"). They are only looked for in a page that has no results, so a real result whose
# snippet mentions chatbots or a challenge is never mistaken for a block.
BLOCK_MARKERS = ("anomaly", "challenge", "bots", "unusual traffic", "captcha")
DDG_HTML_ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")
# The setting (or package) each provider needs. Without it the provider is "not set up" and never called.
NEEDS = {"brave": "brave_api_key", "searxng": "searxng_url", "ddgs": "the ddgs package (pip install ddgs)",
         "ddg_html": ""}

PROVIDER_STATUS: dict[str, dict] = {
    p: {"ok": None, "last_ok": None, "last_error": None, "error": "", "cooldown_until": 0.0} for p in PROVIDERS}
LAST_PROVIDER = ""                                  # the provider that answered the most recent search
SEARCH_FAIL_STREAK = 0                              # searches in a row where every provider failed
LAST_SEARCH_ALERT: dict[int | None, float] = {}     # company id -> epoch of its last "search is down" event
_status_lock = threading.Lock()
_DDGS_INSTALLED: bool | None = None                 # checked once; tests set it explicitly

# A SearXNG you run yourself and the servers tests start never go through the system proxy.
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_proxy_opener = urllib.request.build_opener()


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(iso: str | None) -> str:
    try:
        then = _dt.datetime.strptime(iso or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return "at an unknown time"
    mins = max(0, int((_dt.datetime.now(_dt.timezone.utc) - then).total_seconds() // 60))
    if mins < 1:
        return "just now"
    if mins < 120:
        return f"{mins} min ago"
    return f"{mins // 60} h ago"


def _settings(settings: dict | None) -> dict:
    if settings is None:
        from . import db   # lazy: web.py has no database dependency otherwise, and tests import it alone
        settings = db.get_settings()
    return settings


def _is_local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 20,
          direct: bool = False) -> tuple[int, str]:
    """One request, returning (status, body text).

    HTTP error statuses come back as a status instead of an exception so a provider can tell a block
    (202/403/429) from a network failure. Network failures raise ProviderError. Tests replace this function.
    """
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    opener = _direct_opener if direct or _is_local(url) else _proxy_opener
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except urllib.error.URLError as e:
        raise ProviderError(f"network error: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise ProviderError("timed out") from None
    except (ConnectionError, OSError, ValueError) as e:
        raise ProviderError(f"network error: {e}") from None


def _ddgs_installed() -> bool:
    """True when the optional ddgs package is importable. Looked up once per process."""
    global _DDGS_INSTALLED
    if _DDGS_INSTALLED is None:
        try:
            _DDGS_INSTALLED = importlib.util.find_spec("ddgs") is not None
        except (ImportError, ValueError):
            _DDGS_INSTALLED = False
    return _DDGS_INSTALLED


def _configured(name: str, settings: dict) -> bool:
    """Whether a provider has what it needs. An unconfigured provider is skipped, never counted as failed."""
    if name == "brave":
        return bool((settings.get("brave_api_key") or "").strip())
    if name == "searxng":
        return bool((settings.get("searxng_url") or "").strip())
    if name == "ddgs":
        return _ddgs_installed()
    return True


def _order(settings: dict) -> list[str]:
    """Chain order: the configured provider first, then brave, searxng, ddgs, ddg_html.

    "duckduckgo" means ddgs then ddg_html. The environment variable HOLDCO_SEARCH_PROVIDERS (comma separated)
    restricts the chain, which the end-to-end tests use to stay off the public internet.
    """
    chosen = (settings.get("search_provider") or "duckduckgo").strip().lower()
    first = {"duckduckgo": ["ddgs", "ddg_html"], "brave": ["brave"], "searxng": ["searxng"]}.get(chosen, [])
    order: list[str] = []
    for p in first + list(PROVIDERS):
        if p not in order:
            order.append(p)
    allowed = {p.strip() for p in os.environ.get("HOLDCO_SEARCH_PROVIDERS", "").split(",") if p.strip()}
    if allowed:
        order = [p for p in order if p in allowed]
    return order


def _record(name: str, ok: bool, error: str = "", cooldown: bool = True) -> None:
    """Remember how a provider's last call went. A failure starts the cooldown; a success clears it."""
    with _status_lock:
        st = PROVIDER_STATUS[name]
        if ok:
            st.update({"ok": True, "last_ok": _stamp(), "error": "", "cooldown_until": 0.0})
        else:
            st.update({"ok": False, "last_error": _stamp(), "error": error,
                       "cooldown_until": time.time() + COOLDOWN_SECONDS if cooldown else 0.0})


def reset_status() -> None:
    """Forget every provider's history (tests, and when the owner changes the search settings)."""
    global LAST_PROVIDER, SEARCH_FAIL_STREAK
    with _status_lock:
        for st in PROVIDER_STATUS.values():
            st.update({"ok": None, "last_ok": None, "last_error": None, "error": "", "cooldown_until": 0.0})
        LAST_SEARCH_ALERT.clear()
        LAST_PROVIDER = ""
        SEARCH_FAIL_STREAK = 0


# ---- providers: each returns [{title, url, snippet}], an empty list for a real "no results", or raises
# ---- ProviderError(reason) when it could not answer. settings is passed so every provider has one signature.

def _brave(query: str, n: int, settings: dict) -> list[dict]:
    key = (settings.get("brave_api_key") or "").strip()
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": n})
    status, body = _http(url, headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=25)
    if status in (401, 403):
        raise ProviderError(f"rejected (HTTP {status}, check brave_api_key)")
    if status == 429:
        raise ProviderError("rate limited (HTTP 429)")
    if status >= 400:
        raise ProviderError(f"HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderError("not JSON") from None
    rows = (data.get("web") or {}).get("results") or [] if isinstance(data, dict) else []
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in rows[:n]]


def _searxng(query: str, n: int, settings: dict) -> list[dict]:
    base = (settings.get("searxng_url") or "").strip().rstrip("/")
    url = f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    # SearXNG usually runs on your own machine, so this skips the public-URL check and the system proxy.
    status, body = _http(url, timeout=25, direct=True)
    if status == 403:
        raise ProviderError("HTTP 403 (enable the json format in SearXNG's settings.yml)")
    if status >= 400:
        raise ProviderError(f"HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderError("not JSON (is searxng_url a SearXNG instance?)") from None
    rows = data.get("results") or [] if isinstance(data, dict) else []
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in rows[:n]]


def _ddgs(query: str, n: int, settings: dict) -> list[dict]:
    try:  # optional, more robust DuckDuckGo client: pip install ddgs
        from ddgs import DDGS  # type: ignore
    except ImportError:
        raise ProviderError("not installed") from None
    try:
        rows = DDGS().text(query, max_results=n) or []
    except Exception as e:  # the package raises its own exception types for rate limits and timeouts
        msg = (str(e) or type(e).__name__)[:120]
        if "ratelimit" in msg.lower().replace(" ", "") or any(m in msg.lower() for m in BLOCK_MARKERS):
            raise ProviderError(f"blocked ({msg})") from None
        raise ProviderError(f"{type(e).__name__}: {msg}") from None
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in rows]


def _ddg_block_reason(status: int, html: str) -> str:
    """Why a DuckDuckGo page without results counts as a block, or "" when it does not."""
    if status in BLOCK_STATUSES:
        return f"blocked (HTTP {status} challenge)"
    low = html.lower()
    for marker in BLOCK_MARKERS:
        if marker in low:
            return f"blocked (challenge page mentions '{marker}')"
    return ""


def _ddg_html(query: str, n: int, settings: dict | None = None) -> list[dict]:
    """DuckDuckGo's html and lite pages, no key needed. Raises ProviderError when neither endpoint answers."""
    data = urllib.parse.urlencode({"q": query}).encode()
    reasons: list[str] = []
    for url in DDG_HTML_ENDPOINTS:
        try:
            status, html = _http(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        except ProviderError as e:
            reasons.append(str(e))
            continue
        if status in BLOCK_STATUSES:
            reasons.append(_ddg_block_reason(status, html))
            continue
        if status >= 400:
            reasons.append(f"HTTP {status}")
            continue
        p = _DDGParser()
        try:
            p.feed(html)
        except Exception:  # malformed HTML: keep what we got
            pass
        res = [r for r in p.results if r["url"].startswith("http") and "duckduckgo.com" not in r["url"]]
        if res:
            return [{k: " ".join(v.split()) for k, v in r.items()} for r in res[:n]]
        if "no-results" in html or "no results" in html.lower():
            return []   # a real empty answer, not a failure
        reasons.append(_ddg_block_reason(status, html) or "no results parsed (page layout changed?)")
    # A block is the thing the owner needs to hear about, so it wins over a plain error on the other endpoint.
    blocked = [r for r in reasons if r.startswith("blocked")]
    raise ProviderError(blocked[0] if blocked else (reasons[-1] if reasons else "no results"))


def search(query: str, n: int, settings: dict | None = None, ignore_cooldown: bool = False) -> list[dict]:
    """Ask the providers in chain order and return the first non-empty result list.

    A provider that is not set up is skipped without a call; one that is cooling down after a failure is
    skipped too unless ignore_cooldown is set (the deep Doctor check). Raises WebError listing every provider
    with its reason when none could answer.
    """
    global LAST_PROVIDER, SEARCH_FAIL_STREAK
    settings = _settings(settings)
    n = max(1, min(int(n or 6), 10))
    reasons: list[str] = []
    answered = False
    for name in _order(settings):
        if not _configured(name, settings):
            reasons.append(f"{name}: not set up")
            continue
        st = PROVIDER_STATUS[name]
        if not ignore_cooldown and st["cooldown_until"] > time.time():
            left = max(1, round((st["cooldown_until"] - time.time()) / 60))
            reasons.append(f"{name}: {st['error'] or 'failed'} (cooling down, retry in {left} min)")
            continue
        try:
            rows = globals()["_" + name](query, n, settings)   # looked up at call time so tests can swap providers
        except ProviderError as e:
            _record(name, False, str(e))
            reasons.append(f"{name}: {e}")
            continue
        except Exception as e:   # a bug in one provider must not take the whole chain down
            reason = f"{type(e).__name__}: {str(e)[:120]}"
            _record(name, False, reason)
            reasons.append(f"{name}: {reason}")
            continue
        rows = [r for r in (rows or []) if r.get("url")]
        _record(name, True)      # it answered, so it is healthy even when the query had no hits
        LAST_PROVIDER = name
        answered = True
        if rows:
            SEARCH_FAIL_STREAK = 0
            return rows[:n]
        reasons.append(f"{name}: no results")
    if answered:
        SEARCH_FAIL_STREAK = 0
        return []
    SEARCH_FAIL_STREAK += 1
    raise WebError("All search providers failed: " + ("; ".join(reasons) or "no providers in the chain"))


def search_health(settings: dict | None = None) -> list[dict]:
    """One row per provider in chain order, for the Doctor: {provider, configured, status, detail}."""
    settings = _settings(settings)
    now = time.time()
    rows = []
    for name in _order(settings):
        st = PROVIDER_STATUS[name]
        row = {"provider": name, "configured": _configured(name, settings), "status": "unknown", "detail": ""}
        if not row["configured"]:
            row["detail"] = f"not set up (needs {NEEDS[name]})"
        elif st["ok"] is None:
            row["detail"] = "not tried yet"
        elif st["ok"]:
            row["status"], row["detail"] = "ok", f"ok {_ago(st['last_ok'])}"
        elif st["cooldown_until"] > now:
            left = max(1, round((st["cooldown_until"] - now) / 60))
            row["status"], row["detail"] = "bad", f"{st['error']}, retry in {left} min"
        else:
            row["status"], row["detail"] = "warn", f"{st['error']} ({_ago(st['last_error'])}), will be retried"
        rows.append(row)
    return rows


def live_search_test(settings: dict | None = None) -> tuple[bool, str]:
    """One real query through the chain with cooldowns ignored. Only the deep Doctor run calls this."""
    try:
        rows = search("local business software pricing", 3, _settings(settings), ignore_cooldown=True)
    except WebError as e:
        return False, str(e)
    if not rows:
        return True, f"{LAST_PROVIDER or 'a provider'} answered but found nothing for the test query"
    return True, f"{LAST_PROVIDER} answered with {len(rows)} results"


def search_alert_due(company_id: int | None, interval: float = ALERT_INTERVAL) -> bool:
    """True at most once per company per interval, so the owner hears about an outage once, not per search."""
    now = time.time()
    with _status_lock:
        if now - LAST_SEARCH_ALERT.get(company_id, 0.0) < interval:
            return False
        LAST_SEARCH_ALERT[company_id] = now
        return True
