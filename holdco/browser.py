"""browser_check (Phase 1, docs/phases/phase-1-connect.md section 8): open a page of the company site, or a public
URL, and say what a visitor would meet.

With a healthy Playwright connection the page is rendered: console errors, an accessibility snapshot, a desktop
and a phone-sized screenshot saved under <company>/.qa/, every same-site link fetched through the preview, and every
form checked for a target. Without it (no connection, its health not ok, or any MCP error) the check is static and
its first line says so: the preview fetch status, checks.audit_page, and the same link and form checks done on the
HTML. When even the preview is unreachable (unit tests without a server) the file is read from disk.

The local preview (http://127.0.0.1:<port>/preview/<slug>/...) is reached only from here: browser.py fetches it with
urllib directly and hands it to Playwright, without web.check_public_url (which blocks this PC for agents).
External pages go through web.check_public_url and are never clicked or submitted.
"""
from __future__ import annotations

import base64
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import checks, config, web, workspace
from .tools import Ctx, S, ToolError, _log, tool

PREVIEW_TIMEOUT = 10     # seconds per preview request
LINK_LIMIT = 40          # same-site links checked per page
MOBILE = (390, 844)
DESKTOP = (1280, 800)
HTML_SUFFIXES = (".html", ".htm")
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:")
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the preview never goes through a proxy


@tool("browser_check", "Open a page of the company site (a path in site/) or a public URL and check it: status, "
      "title, console errors, links, forms and screenshots.",
      {"path_or_url": S("A path in site/, e.g. site/index.html, or an http(s) URL"),
       "mobile": {"type": "boolean", "description": "Also check a phone-sized screen (default true)"}},
      ("path_or_url",), risk="read")
def browser_check(ctx: Ctx, path_or_url: str, mobile: bool = True) -> str:
    target = (path_or_url or "").strip()
    if not target:
        raise ToolError("Give a path in site/ (e.g. site/index.html) or an http(s) URL.")
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) and not re.match(r"^[a-z]:[\\/]", target, re.I):
        if not re.match(r"^https?://", target, re.I):
            raise ToolError("Only http(s) URLs can be checked.")
        own = _preview_rel(ctx.company, target)
        if own is None:
            _log(ctx, "tool", f"browser check {target[:120]}")
            return _external(ctx, target)
        target = "site/" + own
    page, rel = _site_page(ctx.company, target)
    _log(ctx, "tool", f"browser check site/{rel}")
    return _check_site_page(ctx, page, rel, mobile)


# ---------------------------------------------------------------- paths and the preview

def _preview_host() -> str:
    host = (config.HOST or "").strip()
    return host if host and host not in ("0.0.0.0", "::", "localhost", "127.0.0.1") else "127.0.0.1"


def _preview_base(company: dict) -> str:
    return f"http://{_preview_host()}:{config.PORT}/preview/{urllib.parse.quote(company['slug'])}/"


def _preview_url(company: dict, rel: str) -> str:
    """http://127.0.0.1:<config.PORT>/preview/<slug>/<path relative to site/>."""
    return _preview_base(company) + urllib.parse.quote(rel)


def _preview_rel(company: dict, url: str) -> str | None:
    """The site/ path of a URL that points at this company's own preview, else None."""
    p = urllib.parse.urlparse(url)
    if (p.hostname or "") not in ("127.0.0.1", "localhost", _preview_host()) or p.port != config.PORT:
        return None
    prefix = f"/preview/{company['slug']}/"
    path = urllib.parse.unquote(p.path)
    if path.rstrip("/") == prefix.rstrip("/"):
        return ""
    return path[len(prefix):] if path.startswith(prefix) else None


def _site_page(company: dict, target: str) -> tuple[Path, str]:
    t = re.sub(r"^(\./)+", "", target.replace("\\", "/").strip()).lstrip("/")
    if t in ("", "site"):
        t = "site/"
    first = t.split("/", 1)[0]
    if first != "site":
        if first in config.WORKSPACE_DIRS:
            raise ToolError(f"browser_check opens pages in site/ (the company website) or public URLs; {t} is not "
                            f"in site/. Use read_file or audit_page for other files.")
        t = "site/" + t
    p = workspace.safe_path(company, t)
    site = workspace.safe_path(company, "site")
    if p != site and site not in p.parents:
        raise ToolError("browser_check opens pages in site/ (the company website) or public URLs.")
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        raise ToolError(f"{workspace.rel(company, p)} doesn't exist. Write the page first, or find it with "
                        f"list_files.")
    return p, p.resolve().relative_to(site.resolve()).as_posix()


def _page_name(rel: str) -> str:
    stem = re.sub(r"\.html?$", "", rel, flags=re.I).strip("/") or "index"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "index"


def _fetch(url: str) -> tuple[int, str, str]:
    """GET through the dashboard's own server: (status, text, error). Status 0 means unreachable."""
    headers = {"User-Agent": "Holdco browser_check"}
    if config.PASSWORD:   # the preview sits behind the dashboard password when one is set
        token = base64.b64encode(f"holdco:{config.PASSWORD}".encode("utf-8")).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with _direct.open(req, timeout=PREVIEW_TIMEOUT) as r:
            return r.status, r.read(web.MAX_BYTES).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, ""
    except urllib.error.URLError as e:
        return 0, "", str(e.reason)
    except (OSError, ValueError) as e:
        return 0, "", str(e) or type(e).__name__


# ---------------------------------------------------------------- shared checks

def _parse(html: str):
    p = checks._PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:   # malformed HTML: keep what was parsed
        pass
    return p


def _first_h1(parser) -> str:
    return next((h[1] for h in parser.headings if h[0] == 1), "")


def _check_links(hrefs: list[str], page_url: str, base: str, site: Path,
                 over_http: bool) -> tuple[int, list[str], int]:
    """Same-site links must answer < 400 through the preview (or exist on disk when there is no preview).
    Returns (checked, problems, external links not checked)."""
    seen: set[str] = set()
    problems: list[str] = []
    checked = external = 0
    base_parts = urllib.parse.urlparse(base)
    for href in hrefs:
        h = (href or "").strip()
        if not h or h.startswith("#") or h.lower().startswith(SKIP_SCHEMES):
            continue
        if h.startswith("/") and not h.startswith("//"):
            absolute = base + h.lstrip("/")          # a root-relative link means the site's root
        else:
            absolute = urllib.parse.urljoin(page_url, h)
        absolute = absolute.split("#", 1)[0]
        parts = urllib.parse.urlparse(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc != base_parts.netloc:
            external += 1
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        if len(seen) > LINK_LIMIT:
            break
        checked += 1
        if not absolute.startswith(base):
            problems.append(f"{h} points outside the site folder")
            continue
        if over_http:
            status, _, err = _fetch(absolute)
            if not status or status >= 400:
                problems.append(f"{h} -> {status or 'unreachable'}{f' ({err})' if err else ''}")
        else:
            sub = urllib.parse.unquote(parts.path[len(base_parts.path):])
            target = (site / sub).resolve() if sub else site.resolve()
            inside = target == site.resolve() or site.resolve() in target.parents
            if not inside or not (target.is_file() or (target / "index.html").is_file()
                                  or target.with_suffix(".html").is_file()):
                problems.append(f"{h} -> missing file")
    return checked, problems, external


def _link_lines(checked: int, problems: list[str], external: int) -> list[str]:
    line = f"Links: {checked} checked, {checked - len(problems)} ok"
    if external:
        line += f" ({external} external not checked; audit_page with check_external_links checks them)"
    return [line] + [f"- broken: {p}" for p in problems]


def _form_lines(parser) -> list[str]:
    forms = parser.forms
    targets, missing = [], []
    for i, f in enumerate(forms, 1):
        kinds = []
        action = (f.get("action") or "").strip()
        if action and action != "#":
            kinds.append("action")
        if "data-netlify" in f or "netlify" in f:
            kinds.append("data-netlify")
        if kinds:
            targets.append(", ".join(kinds))
        else:
            name = f.get("name") or f.get("id") or ""
            missing.append(f"- form {i}{f' ({name})' if name else ''} has no action or data-netlify attribute: "
                           f"submissions go nowhere")
    line = f"Forms: {len(forms)} found, {len(targets)} with a target"
    if targets:
        line += f" ({'; '.join(sorted(set(targets)))})"
    return [line] + missing


def _audit_lines(page: Path, site: Path) -> list[str]:
    try:
        report = checks.audit_page(page, site, False)
    except Exception as e:   # the audit is extra information; never lose the rest of the check over it
        return [f"Audit: failed ({type(e).__name__}: {e})"]
    head, *rest = report.splitlines() or [""]
    m = re.search(r"(\d+) blocker\(s\), (\d+) major, (\d+) minor", head)
    if not m:
        return [f"Audit: {head}"] + rest
    return [f"Audit: {m.group(1)} blocker(s), {m.group(2)} major, {m.group(3)} minor (details below)"] + rest


# ---------------------------------------------------------------- Playwright

def _playwright(company: dict) -> tuple[dict | None, str]:
    """The Playwright connection to use, or None with the reason for the static check's first line."""
    try:
        from . import connectors   # imported here: connectors imports tools, which imports this module
        inst = connectors.instance("playwright", company)
    except Exception:
        return None, "Playwright is not set up"
    if not inst or not inst.get("enabled", 1):
        return None, "Playwright is not set up"
    status = inst.get("status") or ""
    if not status:
        return None, "Playwright is not set up (its card has not been tested yet)"
    if status != "ok":
        return None, f"Playwright failed: {inst.get('detail') or status}"
    return inst, ""


def _content(result) -> tuple[str, list[dict]]:
    """Text and image blocks of an MCP tool result."""
    texts, images = [], []
    blocks = (result.get("content") or []) if isinstance(result, dict) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "image" and block.get("data"):
            images.append(block)
    return "\n".join(texts), images


class _Browser:
    """Thin wrapper over the connection's MCP client: any error result raises, so the check falls back."""

    def __init__(self, inst: dict):
        from . import connectors
        self.client = connectors.client(inst)

    def call(self, name: str, args: dict | None = None) -> tuple[str, list[dict]]:
        result = self.client.call_tool(name, args or {})
        text, images = _content(result)
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(f"{name}: {text.strip()[:300] or 'error'}")
        return text, images

    def screenshot(self, path: Path, company: dict) -> str:
        text, images = self.call("browser_take_screenshot", {"type": "png"})
        path.parent.mkdir(parents=True, exist_ok=True)
        if images:
            path.write_bytes(base64.b64decode(images[0]["data"]))
            return workspace.rel(company, path)
        # Some versions save the file themselves and name it in the text.
        for found in re.findall(r"([A-Za-z]:\\[^\s'\"`]+\.png|/[^\s'\"`]+\.png)", text):
            if Path(found).is_file():
                shutil.copyfile(found, path)
                return workspace.rel(company, path)
        return f"{workspace.rel(company, path)} not saved (the server returned no image)"


def _console_errors(text: str) -> list[str]:
    errors = []
    for line in (text or "").splitlines():
        s = line.strip().lstrip("- ").strip()
        if re.match(r"^\[(error|pageerror)\]", s, re.I) or re.match(r"^(error|uncaught)\b", s, re.I):
            errors.append(s[:200])
    return errors


def _snapshot_title(snap: str) -> str:
    m = re.search(r"Page Title:\s*(.+)", snap or "")
    return m.group(1).strip() if m else ""


def _snapshot_h1(snap: str) -> str:
    m = re.search(r'heading "((?:[^"\\]|\\.)*)" \[level=1\]', snap or "")
    return m.group(1).replace('\\"', '"') if m else ""


def _snapshot_links(snap: str) -> list[str]:
    return [u.strip().strip('"') for u in re.findall(r"/url:\s*(\S+)", snap or "")]


def _with_playwright(ctx: Ctx, inst: dict, page: Path, rel: str, url: str, mobile: bool) -> str:
    company = ctx.company
    site = workspace.safe_path(company, "site")
    b = _Browser(inst)
    desktop = {"width": DESKTOP[0], "height": DESKTOP[1]}
    try:
        b.call("browser_resize", desktop)   # a previous phone-sized check may have left the window small
    except Exception:
        pass   # resizing is a nicety; navigation and the snapshot below decide whether rendering works
    b.call("browser_navigate", {"url": url})
    console, _ = b.call("browser_console_messages", {})
    snap, _ = b.call("browser_snapshot", {})
    qa = workspace.root(company) / ".qa"
    name = _page_name(rel)
    shots = [b.screenshot(qa / f"{name}.png", company)]
    if mobile:
        try:
            b.call("browser_resize", {"width": MOBILE[0], "height": MOBILE[1]})
            shots.append(b.screenshot(qa / f"{name}-mobile.png", company))
        except Exception as e:   # keep the desktop results; say what is missing
            shots.append(f"no phone-sized screenshot ({_reason(e)})")
        try:
            b.call("browser_resize", desktop)
        except Exception:
            pass
    status, html, err = _fetch(url)
    served = 0 < status < 400
    if not served:
        html = page.read_text(encoding="utf-8", errors="replace")
    parser = _parse(html)
    hrefs = _snapshot_links(snap) + [a.get("href") or "" for a in parser.anchors]
    checked, problems, external = _check_links(hrefs, url, _preview_base(company), site, served)
    errors = _console_errors(console)
    lines = [f"Browser check (Playwright) of site/{rel}",
             f"GET {url} -> {status or f'unreachable ({err})'}",
             f"Title: {_snapshot_title(snap) or ' '.join(parser.title.split()) or '(none)'}",
             f"H1: {_snapshot_h1(snap) or _first_h1(parser) or '(none)'}",
             f"Console errors: {len(errors) or 'none'}"]
    lines += [f"- {e}" for e in errors[:10]]
    if page.suffix.lower() in HTML_SUFFIXES:
        lines += _link_lines(checked, problems, external) + _form_lines(parser)
    lines.append(f"Screenshots: {', '.join(shots)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the site page, with or without Playwright

def _check_site_page(ctx: Ctx, page: Path, rel: str, mobile: bool) -> str:
    url = _preview_url(ctx.company, rel)
    inst, why = _playwright(ctx.company)
    if inst is not None:
        try:
            return _with_playwright(ctx, inst, page, rel, url, mobile)
        except Exception as e:   # any MCP problem: the static check, labelled with the reason
            why = f"Playwright failed: {_reason(e)}"
    return _static(ctx, page, rel, url, why)


def _reason(e: Exception) -> str:
    text = " ".join(str(e).split()) or type(e).__name__
    return text[:240]


def _static(ctx: Ctx, page: Path, rel: str, url: str, why: str) -> str:
    company = ctx.company
    site = workspace.safe_path(company, "site")
    status, html, err = _fetch(url)
    lines = [f"Static check only: {why}"]
    served = 0 < status < 400
    if served:
        lines.append(f"GET {url} -> {status}")
    elif status:
        lines.append(f"GET {url} -> {status}; checked site/{rel} on disk instead")
    else:
        lines.append(f"GET {url} -> unreachable ({err}); read site/{rel} from disk")
    if page.suffix.lower() not in HTML_SUFFIXES:
        lines.append("Not an HTML page: only its status was checked.")
        return "\n".join(lines)
    if not served:
        html = page.read_text(encoding="utf-8", errors="replace")
    parser = _parse(html)
    lines.append(f"Title: {' '.join(parser.title.split()) or '(none)'}")
    lines.append(f"H1: {_first_h1(parser) or '(none)'}")
    hrefs = [a.get("href") or "" for a in parser.anchors]
    lines += _link_lines(*_check_links(hrefs, url, _preview_base(company), site, served))
    lines += _form_lines(parser)
    lines += _audit_lines(page, site)
    return "\n".join(lines)


# ---------------------------------------------------------------- external pages

def _render_external(inst: dict, url: str) -> list[str]:
    """Open a public page in the browser for its console errors. Nothing is clicked, typed or submitted."""
    b = _Browser(inst)
    b.call("browser_navigate", {"url": url})
    console, _ = b.call("browser_console_messages", {})
    return _console_errors(console)


def _external(ctx: Ctx, url: str) -> str:
    web.check_public_url(url)   # this PC and the home network stay off limits (HOLDCO_ALLOW_LOCAL: tests only)
    status, ctype, body, final = web.http_get(url)
    text = web._decode(body, ctype)
    title, content, _ = web.html_to_text(text, final)
    has_form = bool(re.search(r"<form[\s>]", text, re.I))
    inst, why = _playwright(ctx.company)
    errors = None
    if inst is not None:
        try:
            errors = _render_external(inst, url)
        except Exception as e:
            why = f"Playwright failed: {_reason(e)}"
    lines = [f"Browser check (Playwright) of {url}" if errors is not None else f"Static check only: {why}",
             f"GET {url} -> {status}" + (f" (ended at {final})" if final and final != url else ""),
             f"Title: {title or '(none)'}",
             "Form: form found" if has_form else "Form: no form on the page"]
    if errors is not None:
        lines.append(f"Console errors: {len(errors) or 'none'}")
        lines += [f"- {e}" for e in errors[:10]]
    snippet = " ".join(re.sub(r"(?m)^(#+|-) ", "", content).split())[:300]   # without the heading and list marks
    lines.append(f"Text: {snippet or '(no text)'}")
    lines.append("Nothing was clicked or submitted on this page.")
    if status < 400:
        try:   # the page was really fetched, so it can back a claim like any fetch_url page
            from . import truth
            truth.cache_page(ctx, url, f"Title: {title}\nURL: {final}\n\n{content[:9000]}")
        except Exception:
            pass
    return "\n".join(lines)
