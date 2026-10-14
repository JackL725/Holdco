"""Web search and page fetching, standard library only.

Search providers: DuckDuckGo (default, no key; uses the `ddgs` package if installed, else the HTML page),
a SearXNG instance you run yourself, or the Brave Search API.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
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

def check_public_url(url: str) -> str:
    """Agents may only reach the public internet, never this PC or your home network."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError("Only http(s) URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise WebError("URL has no host.")
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
        return f"HTTP {status} fetching {url}\n{text[:500]}"
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


def _ddg_html(query: str, n: int) -> list[dict]:
    for url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA,
                                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read(MAX_BYTES).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        p = _DDGParser()
        p.feed(html)
        res = [r for r in p.results if r["url"].startswith("http") and "duckduckgo.com" not in r["url"]]
        if res:
            return [{k: " ".join(v.split()) for k, v in r.items()} for r in res[:n]]
    return []


def search(query: str, n: int, settings: dict) -> list[dict]:
    n = max(1, min(int(n or 6), 10))
    provider = settings.get("search_provider", "duckduckgo")
    if provider == "searxng" and settings.get("searxng_url"):
        base = settings["searxng_url"].rstrip("/")
        url = f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        # SearXNG usually runs on your own machine, so this skips the public-URL check.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in data.get("results", [])[:n]]
    if provider == "brave" and settings.get("brave_api_key"):
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": n})
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "X-Subscription-Token": settings["brave_api_key"]})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                for r in (data.get("web") or {}).get("results", [])[:n]]
    try:  # optional, more robust DuckDuckGo client: pip install ddgs
        from ddgs import DDGS  # type: ignore
        rows = DDGS().text(query, max_results=n) or []
        out = [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in rows]
        if out:
            return out
    except Exception:
        pass
    return _ddg_html(query, n)
