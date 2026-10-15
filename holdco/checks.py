"""Deterministic quality checks agents can run on their own work.

Research on LLM reviewers is consistent: critiques grounded in tool output (parsing, measuring, fetching)
catch real problems, while a model re-reading its own work mostly doesn't. These checks give agents
evidence instead of opinions.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from . import web

# ======================= colour contrast (WCAG 2.2) =======================

NAMED = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
         "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "navy": "#000080", "orange": "#ffa500",
         "yellow": "#ffff00", "purple": "#800080", "teal": "#008080", "maroon": "#800000"}


def parse_color(s: str) -> tuple[float, float, float]:
    s = (s or "").strip().lower()
    s = NAMED.get(s, s)
    m = re.fullmatch(r"#?([0-9a-f]{3,8})", s)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        if len(h) in (6, 8):
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+).*\)", s)
    if m:
        return tuple(float(x) for x in m.groups())  # type: ignore[return-value]
    raise ValueError(f"Can't read the colour '{s}'. Use hex like #1a2b3c or rgb(26, 43, 60).")


def _lum(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(parse_color(fg)), _lum(parse_color(bg))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def contrast_report(fg: str, bg: str, label: str = "") -> str:
    r = contrast(fg, bg)
    # WCAG doesn't round: 4.499 fails 4.5.
    res = [f"{label + ': ' if label else ''}{fg} on {bg} = {r:.2f}:1",
           f"  normal text (needs 4.5:1): {'pass' if r >= 4.5 else 'FAIL'}",
           f"  large text 24px+ or bold 18.5px+ (3:1): {'pass' if r >= 3 else 'FAIL'}",
           f"  buttons, borders, focus rings (3:1): {'pass' if r >= 3 else 'FAIL'}"]
    return "\n".join(res)


# ======================= page audit =======================

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = None
        self.metas: list[dict] = []
        self.links: list[dict] = []
        self.title = ""
        self._in = []
        self.headings: list[tuple[int, str]] = []
        self.imgs: list[dict] = []
        self.inputs: list[dict] = []
        self.labels_for: set[str] = set()
        self.label_depth = 0
        self.buttons: list[dict] = []
        self.anchors: list[dict] = []
        self.forms: list[dict] = []
        self.scripts: list[dict] = []
        self.styles: list[str] = []
        self.text: list[str] = []
        self._cur_button = None
        self._cur_anchor = None
        self._cur_heading = None
        self._cur_script = None
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.links.append(a)
        elif tag == "title":
            self._in.append("title")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._cur_heading = [int(tag[1]), ""]
        elif tag == "img":
            self.imgs.append(a)
            if self._cur_anchor is not None and a.get("alt"):
                self._cur_anchor["text"] += a["alt"]
            if self._cur_button is not None and a.get("alt"):
                self._cur_button["text"] += a["alt"]
        elif tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            if not (tag == "input" and a.get("type", "text").lower() in ("hidden", "submit", "button", "image", "reset")):
                self.inputs.append({**a, "_tag": tag, "_in_label": self.label_depth > 0})
            elif tag == "input" and a.get("type", "").lower() in ("submit", "button"):
                self.buttons.append({**a, "text": a.get("value", "")})
        elif tag == "button":
            self._cur_button = {**a, "text": ""}
        elif tag == "a":
            self._cur_anchor = {**a, "text": ""}
        elif tag == "form":
            self.forms.append(a)
        elif tag == "script":
            self._cur_script = {**a, "body": ""}
        elif tag == "style":
            self._in_style = True
        if a.get("style"):
            self.styles.append(a["style"])

    def handle_endtag(self, tag):
        if tag == "title" and "title" in self._in:
            self._in.remove("title")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._cur_heading:
            self.headings.append((self._cur_heading[0], " ".join(self._cur_heading[1].split())))
            self._cur_heading = None
        elif tag == "label" and self.label_depth:
            self.label_depth -= 1
        elif tag == "button" and self._cur_button is not None:
            self.buttons.append(self._cur_button)
            self._cur_button = None
        elif tag == "a" and self._cur_anchor is not None:
            self.anchors.append(self._cur_anchor)
            self._cur_anchor = None
        elif tag == "script" and self._cur_script is not None:
            self.scripts.append(self._cur_script)
            self._cur_script = None
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if "title" in self._in:
            self.title += data
        if self._cur_script is not None:
            self._cur_script["body"] += data
            return
        if self._in_style:
            self.styles.append(data)
            return
        if self._cur_heading is not None:
            self._cur_heading[1] += data
        if self._cur_button is not None:
            self._cur_button["text"] += data
        if self._cur_anchor is not None:
            self._cur_anchor["text"] += data
        self.text.append(data)


# ======================= placeholders (Phase 0.5, design 2.6) =======================
# The first real landing page shipped with gtag id G-PLACEHOLDER and a 1x1 data-URI social image. Each pattern
# below is something a template or a model leaves behind that must never reach the public.

_PH_LINE = [
    (re.compile(r"\b(?:G|UA|GTM|AW|DC)-(?:PLACEHOLDER|X{3,}[\w-]*|0{4,}[\w-]*|YOUR[\w-]*|TEST[\w-]*|1234[\w-]*)\b",
                re.I), "analytics id {0}"),
    # Upper case only: CSS for form hints (input::placeholder) is common and harmless.
    (re.compile(r"(?<![\w-])PLACEHOLDER(?![\w-])"), "the word {0}"),
    (re.compile(r"\[placeholder\]", re.I), "placeholder text {0}"),
    (re.compile(r"lorem\s+ipsum", re.I), "placeholder text '{0}'"),
    (re.compile(r"\b(?:TODO|TBD|FIXME)\b"), "'{0}' marker"),
    (re.compile(r"\byour text here\b", re.I), "placeholder text '{0}'"),
    (re.compile(r"\{\{\s*[\w.\- ]{1,40}?\s*\}\}"), "template token {0}"),
    (re.compile(r"\[(?:your |company |business |first |last |full |customer |client )?(?:name|company|company name|"
                r"business name|email|e-mail|phone|phone number|address|city|date|link|url|website|title|product|"
                r"price|location)\](?![(\[:])", re.I), "template token {0}"),
    (re.compile(r"\bYOUR_[A-Z0-9_]+\b"), "template token {0}"),
]
_PH_EMAIL = re.compile(r"[\w.+-]*(?:placeholder|example)[\w.+-]*@[\w-]+(?:\.[\w-]+)*|[\w.+-]+@(?:[\w-]+\.)*"
                       r"example\.(?:com|org|net)\b", re.I)
_PH_DOMAIN = re.compile(r"(?<![\w.-])(?:[\w-]+\.)*example\.(?:com|org|net)\b", re.I)
# The placeholder="..." attribute of a form field is a hint shown in an empty input, not leftover template text.
_PH_ATTR = re.compile(r"""\splaceholder\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)


class _PlaceholderParser(HTMLParser):
    """Social images given as inline data URIs, and links whose destination is just '#'."""

    SOCIAL = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []
        self._a: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in self.SOCIAL and a.get("content", "").strip().lower().startswith("data:"):
                self.found.append((line, f"social image ({key}) is an inline data URI; link previews need a real "
                                         "image URL"))
        elif tag == "a" and (a.get("href") or "").strip() == "#":
            # A '#' link with a script handler or a button role does something; one without is a dead placeholder.
            if not any(k.startswith("on") or k in ("role", "data-action", "data-toggle", "data-bs-toggle") for k in a):
                self._a = {"line": line, "text": ""}

    def handle_data(self, data):
        if self._a is not None:
            self._a["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            text = " ".join(self._a["text"].split())
            if text.lower() not in ("top", "back to top", "to top", "↑"):
                self.found.append((self._a["line"], f"link \"{text[:40] or '(icon)'}\" goes to \"#\" instead of a "
                                                    "real page"))
            self._a = None


def find_placeholders(text: str, is_html: bool = False) -> list[tuple[int, str]]:
    """(line number, what) for every placeholder left in a page or document: analytics ids like G-PLACEHOLDER or
    G-XXXXXXX, placeholder@ and example.com addresses and links, lorem ipsum, TODO/TBD/FIXME, template tokens
    ({{name}}, [Company Name], YOUR_...), and in HTML social images given as data URIs and links to '#'."""
    text = text or ""
    if is_html:
        text = _PH_ATTR.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)   # same length keeps line numbers
    found: list[tuple[int, str]] = []
    for no, line in enumerate(text.splitlines(), 1):
        spans = []
        for m in _PH_EMAIL.finditer(line):
            spans.append(m.span())
            found.append((no, f"placeholder email {m.group(0)}"))
        for m in _PH_DOMAIN.finditer(line):
            if not any(a <= m.start() < b for a, b in spans):
                found.append((no, f"example domain {m.group(0)}"))
        for rx, label in _PH_LINE:
            for m in rx.finditer(line):
                if not any(a <= m.start() < b for a, b in spans):
                    found.append((no, label.format(m.group(0))))
    if is_html:
        p = _PlaceholderParser()
        try:
            p.feed(text)
            p.close()
        except Exception:   # malformed HTML: the line checks above still ran
            pass
        found.extend(p.found)
    out, seen = [], set()
    for item in sorted(found, key=lambda x: x[0]):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _meta(p: _PageParser, key: str, attr: str = "name") -> str | None:
    for m in p.metas:
        if m.get(attr, "").lower() == key:
            return m.get("content", "")
    return None


def audit_page(page: Path, site_root: Path | None, check_external: bool = False) -> str:
    raw = page.read_text(encoding="utf-8", errors="replace")
    p = _PageParser()
    try:
        p.feed(raw)
        p.close()
    except Exception as e:
        return f"BLOCKER: the HTML couldn't be parsed ({e})."
    found: list[tuple[str, str]] = []
    passed: list[str] = []

    def add(sev, msg):
        found.append((sev, msg))

    def ok(msg):
        passed.append(msg)

    text = " ".join(" ".join(p.text).split())
    low = raw.lower()

    # basics
    ok("doctype") if low.lstrip().startswith("<!doctype html") else add("minor", "Missing <!doctype html>.")
    ok("lang") if p.lang else add("major", "<html> has no lang attribute (screen readers need it).")
    ok("charset") if any("charset" in m for m in p.metas) else add("minor", "No <meta charset=\"utf-8\">.")
    vp = _meta(p, "viewport")
    ok("viewport") if vp and "width=device-width" in vp else add("blocker", "No viewport meta tag "
                                                                  "(<meta name=\"viewport\" content=\"width=device-width, "
                                                                  "initial-scale=1\">): the page won't fit phones.")
    title = " ".join(p.title.split())
    if not title:
        add("blocker", "No <title>.")
    elif not 15 <= len(title) <= 65:
        add("minor", f"Title is {len(title)} characters; about 50-60 shows fully in search results.")
    else:
        ok("title")
    desc = _meta(p, "description")
    if not desc:
        add("major", "No meta description.")
    elif not 70 <= len(desc) <= 170:
        add("minor", f"Meta description is {len(desc)} characters; about 150-160 is typical.")
    else:
        ok("meta description")
    canon = next((l.get("href", "") for l in p.links if "canonical" in l.get("rel", "").lower()), None)
    if canon is None:
        add("minor", "No canonical link.")
    elif not canon.startswith("https://"):
        add("minor", "Canonical URL should be an absolute https:// URL.")
    else:
        ok("canonical")
    missing_og = [k for k in ("og:title", "og:description", "og:image", "og:url") if _meta(p, k, "property") is None]
    add("minor", f"Missing Open Graph tags for link previews: {', '.join(missing_og)}.") if missing_og else ok("Open Graph")

    # headings
    h1s = [h for h in p.headings if h[0] == 1]
    if len(h1s) != 1:
        add("major", f"Page has {len(h1s)} h1 headings; use exactly one.")
    else:
        ok("one h1")
    prev = 0
    for level, htext in p.headings:
        if prev and level > prev + 1:
            add("minor", f"Heading level jumps from h{prev} to h{level} at \"{htext[:40]}\".")
            break
        prev = level

    # images
    for i, img in enumerate(p.imgs):
        src = img.get("src", "")[:60]
        if "alt" not in img:
            add("major", f"Image {src} has no alt attribute (use alt=\"\" if decorative).")
        if not (img.get("width") and img.get("height")) and "aspect-ratio" not in img.get("style", ""):
            add("minor", f"Image {src} has no width/height, which causes layout shift (CLS).")
        if i == 0 and img.get("loading", "").lower() == "lazy":
            add("major", f"The first image ({src}) is lazy-loaded; never lazy-load the main (LCP) image.")
    if p.imgs:
        ok(f"{len(p.imgs)} images checked")

    # forms
    for inp in p.inputs:
        has_label = (inp.get("id") and inp["id"] in p.labels_for) or inp["_in_label"] or inp.get("aria-label") \
            or inp.get("aria-labelledby")
        name = inp.get("name") or inp.get("id") or inp["_tag"]
        if not has_label:
            msg = f"Form field '{name}' has no label"
            add("major", msg + (" (a placeholder isn't a label)." if inp.get("placeholder") else "."))
    for f in p.forms:
        netlify = "data-netlify" in f or "netlify" in f
        if not f.get("action") and not netlify:
            add("major", "A form has no action and isn't a Netlify form, so submissions go nowhere.")
        if netlify and not f.get("name"):
            add("major", "Netlify forms need a name attribute.")
        if netlify and "netlify-honeypot" not in f:
            add("minor", "Add netlify-honeypot=\"bot-field\" and a hidden bot-field input to stop spam.")
    if p.inputs and not any(sev in ("major", "blocker") and "Form field" in m for sev, m in found):
        ok("form labels")

    # buttons and links
    for b in p.buttons:
        if not (b.get("text", "").strip() or b.get("aria-label")):
            add("major", "A button has no text or aria-label.")
        elif b.get("text", "").strip().lower() in ("submit", "click here", "go"):
            add("minor", f"Button text \"{b['text'].strip()}\" doesn't say what happens; e.g. \"Join the waitlist\".")
    missing_local = []
    external = []
    for a in p.anchors:
        href = a.get("href")
        label = a.get("text", "").strip() or a.get("aria-label")
        if not label:
            add("major", f"A link to {str(href)[:60]} has no text.")
        elif label.lower() in ("click here", "here", "learn more", "read more"):
            add("minor", f"Link text \"{label}\" doesn't say where it goes.")
        if href is None or href == "":
            add("minor", f"Link \"{(label or '')[:30]}\" has no destination.")
            continue
        if href == "#":
            continue   # a dead '#' link is a placeholder blocker (find_placeholders); one with a handler is fine
        if href.startswith(("http://", "https://")):
            external.append(href)
        elif not href.startswith(("#", "mailto:", "tel:", "javascript:")) and site_root is not None:
            target = (page.parent / href.split("#")[0].split("?")[0]).resolve()
            if href.startswith("/"):
                target = (site_root / href.lstrip("/").split("#")[0].split("?")[0]).resolve()
            if href.split("#")[0] and not (target.exists() or (target / "index.html").exists()
                                            or target.with_suffix(".html").exists()):
                missing_local.append(href)
    for src in [i.get("src", "") for i in p.imgs] + [l.get("href", "") for l in p.links if "stylesheet" in l.get("rel", "")]:
        if src and not src.startswith(("http", "data:", "//")) and site_root is not None:
            target = (site_root / src.lstrip("/")) if src.startswith("/") else (page.parent / src)
            if not target.resolve().exists():
                missing_local.append(src)
    for m in sorted(set(missing_local))[:10]:
        add("major", f"Broken local link or file: {m}")

    if check_external and external:
        bad = []
        for url in list(dict.fromkeys(external))[:15]:
            try:
                web.check_public_url(url)
                status = web.http_get(url, timeout=12)[0]
                if status >= 400:
                    bad.append(f"{url} (HTTP {status})")
            except web.WebError as e:
                bad.append(f"{url} ({e})")
        for b in bad:
            add("major", f"External link doesn't work: {b}")
        if not bad:
            ok(f"{min(len(set(external)), 15)} external links load")

    # scripts, CSS, structured data
    ext_scripts = [s.get("src") for s in p.scripts if s.get("src")]
    if len(ext_scripts) > 5:
        add("minor", f"{len(ext_scripts)} external scripts; each slows the page.")
    head_sync = re.search(r"<head.*?</head>", low, re.S)
    if head_sync and re.search(r"<script(?![^>]*\b(async|defer|type=\"module\"|type=\"application/ld\+json\"))[^>]*src=",
                               head_sync.group(0)):
        add("minor", "A script in <head> loads without async/defer and blocks rendering.")
    css = " ".join(p.styles).lower()
    if re.search(r"outline\s*:\s*(none|0)\b", css) and ":focus-visible" not in css and ":focus" not in css:
        add("major", "CSS removes focus outlines with no replacement; keyboard users can't see where they are.")
    for s in p.scripts:
        if s.get("type", "").lower() == "application/ld+json":
            try:
                data = json.loads(s["body"])
                blob = json.dumps(data).lower()
                if "aggregaterating" in blob or '"review"' in blob:
                    add("major", "Structured data includes ratings/reviews; only allowed for real reviews shown on the page.")
                else:
                    ok("structured data parses")
            except ValueError:
                add("major", "A JSON-LD block isn't valid JSON.")
    for sc in p.scripts:
        body = sc.get("body", "")
        if re.search(r"(sk_live|sk-[a-z0-9]{20,}|api[_-]?key\s*[:=]\s*['\"][a-z0-9_\-]{16,})", body, re.I):
            add("blocker", "Something that looks like a secret API key is in page JavaScript.")

    # content honesty: every placeholder is a blocker (deploy_site refuses while any remains)
    placeholders = find_placeholders(raw, True)
    for line_no, what in placeholders[:20]:
        add("blocker", f"Placeholder text still on the page (line {line_no}): {what}.")
    if len(placeholders) > 20:
        add("blocker", f"Placeholder text: {len(placeholders) - 20} more placeholders not listed.")
    claims = re.findall(r"\b\d[\d,.]*\s*\+?\s*(?:happy\s+)?(?:customers|users|teams|companies|businesses|clients|"
                        r"downloads|members|subscribers)\b", text, re.I)
    for c in claims[:3]:
        add("major", f"Claim \"{c.strip()}\": make sure it's real and current, or remove it.")
    if re.search(r"★★★★|\b[45](\.\d)?\s*/\s*5\b|\b5[- ]star", text):
        add("major", "Star ratings appear on the page: only show real reviews (FTC fake-review rule).")
    if re.search(r"(gdpr|hipaa|wcag|soc ?2|pci)[- ]compliant", text, re.I):
        add("major", "The page claims compliance or certification; only keep it with evidence.")

    # site files
    if site_root is not None:
        if not (site_root / "robots.txt").exists():
            add("minor", "site/robots.txt is missing (include a Sitemap: line).")
        if not (site_root / "sitemap.xml").exists():
            add("minor", "site/sitemap.xml is missing.")

    size_kb = len(raw.encode("utf-8")) / 1024
    if size_kb > 300:
        add("minor", f"The HTML is {size_kb:.0f} KB; keep pages light.")

    order = {"blocker": 0, "major": 1, "minor": 2}
    found.sort(key=lambda x: order[x[0]])
    counts = {k: sum(1 for s, _ in found if s == k) for k in order}
    head = (f"Audit of {page.name}: {counts['blocker']} blocker(s), {counts['major']} major, {counts['minor']} minor. "
            f"{len(text.split())} words of visible text.")
    lines = [head] + [f"{s.upper()}: {m}" for s, m in found]
    if passed:
        lines.append("Passed: " + ", ".join(passed))
    lines.append("Not checked automatically: colour contrast (use check_contrast), real-device speed, how the copy reads.")
    return "\n".join(lines)


# ======================= copy review =======================

HYPE = ["revolutionary", "game-changing", "game changer", "best-in-class", "world-class", "cutting-edge",
        "unparalleled", "next-level", "skyrocket", "supercharge", "effortless", "ultimate", "unlock the power",
        "seamless", "disrupt", "guaranteed", "the best", "number one", "#1", "10x", "magic"]
URGENCY = ["act now", "limited time", "hurry", "last chance", "don't miss out", "only a few left", "expires today",
           "before it's too late", "spots left"]
SUPPORT_AVOID = ["sorry for any inconvenience", "sorry for the inconvenience", "unfortunately", "sorry you feel",
                 "as per our policy", "per our policy"]
CLICHES = ["i hope this email finds you well", "i hope this finds you well", "just following up", "touch base",
           "circle back", "quick question"]
COMPLIANCE = ["gdpr compliant", "wcag compliant", "hipaa compliant", "soc 2 compliant", "fully compliant",
              "certified secure"]
GRADE_TARGET = {"landing": 7, "cold_email": 7, "email": 8, "support_reply": 8, "social": 8, "social_x": 8,
                "article": 9, "general": 8}


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def _plain(text: str, is_html: bool) -> str:
    if is_html:
        return web.html_to_text(text)[1]
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(#+|[-*+]|\d+[.)]|>)\s*", "", text, flags=re.M)
    text = re.sub(r"[*_|]", " ", text)
    return text


def review_text(text: str, kind: str = "general", is_html: bool = False) -> str:
    kind = kind if kind in GRADE_TARGET else "general"
    raw = text
    text = _plain(text, is_html)
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    n = len(words)
    if not n:
        return "No words to review."
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if re.search(r"[A-Za-z]", s)]
    ns = max(1, len(sentences))
    syl = sum(_syllables(w) for w in words)
    grade = 0.39 * (n / ns) + 11.8 * (syl / n) - 15.59
    complex_pct = 100 * sum(1 for w in words if _syllables(w) >= 3) / n
    longest = max(sentences, key=lambda s: len(s.split())) if sentences else ""
    low = text.lower()
    issues: list[str] = []
    target = GRADE_TARGET[kind]
    lines = [f"{n} words, {ns} sentences, average {n / ns:.1f} words per sentence, reading grade {grade:.1f} "
             f"(target {target} or lower for {kind.replace('_', ' ')}), {complex_pct:.0f}% words with 3+ syllables."]
    if grade > target:
        issues.append(f"Reading grade {grade:.1f} is above {target}: shorter sentences and plainer words convert better.")
    if len(longest.split()) > 25:
        issues.append(f"Longest sentence has {len(longest.split())} words: \"{longest.strip()[:90]}...\"")
    hits = [h for h in HYPE if h in low]
    if hits:
        issues.append("Hype words lower credibility: " + ", ".join(hits[:8]))
    hits = [h for h in URGENCY if h in low]
    if hits:
        issues.append("Urgency language (only if it's literally true): " + ", ".join(hits))
    hits = [h for h in COMPLIANCE if h in low]
    if hits:
        issues.append("Compliance claims need evidence: " + ", ".join(hits))
    numbers = sorted(set(re.findall(r"(?<![\w/])\$?\d[\d,]*(?:\.\d+)?\s*%?(?:\s*(?:x|k|m|million|billion))?", text)))
    numbers = [x.strip() for x in numbers if not re.fullmatch(r"(19|20)\d\d", x.strip()) and len(x.strip()) > 0]
    if numbers:
        lines.append("Numbers found (each must be real and sourced): " + ", ".join(numbers[:12]))
    if re.search(r"[\"“][^\"”]{12,}[\"”]\s*[-—–]\s*[A-Z]", raw):
        issues.append("Looks like a testimonial or quote with attribution: only use real ones with permission.")
    caps = [w for w in words if len(w) > 3 and w.isupper()]
    if len(caps) > 2:
        issues.append(f"{len(caps)} words in ALL CAPS read as shouting.")
    if text.count("!") > 2:
        issues.append(f"{text.count('!')} exclamation marks.")

    if kind == "cold_email":
        if n > 80:
            issues.append(f"{n} words: cold emails do best under 80 (aim for 50-80).")
        q = text.count("?")
        if q == 0:
            issues.append("No question: end with one low-pressure ask (\"Worth a look?\").")
        elif q > 2:
            issues.append(f"{q} questions: keep to one clear ask.")
        if not re.search(r"unsubscribe|opt[ -]?out|not interested|won'?t (email|follow up|reach out)|reply .{0,12}\bno\b"
                         r"|let me know if you'?d rather|stop hearing", low):
            issues.append("No opt-out line (e.g. \"Not relevant? Reply 'no' and I won't follow up.\").")
        if len(re.findall(r"https?://", raw)) > 1:
            issues.append("More than one link hurts deliverability; use one or none.")
        hits = [c for c in CLICHES if c in low]
        if hits:
            issues.append("Cliché openers: " + ", ".join(hits))
    if kind == "email" and "unsubscribe" not in low:
        issues.append("Marketing emails need an unsubscribe line (CAN-SPAM).")
    if kind == "support_reply":
        hits = [h for h in SUPPORT_AVOID if h in low]
        if hits:
            issues.append("Phrases to avoid in support (say what you can do instead): " + ", ".join(hits))
        if not re.search(r"\b(ai|assistant)\b", low):
            issues.append("Sign as the company's AI assistant and offer a person.")
        if re.search(r"\b(refund|discount|credit|free month)\b", low):
            issues.append("Mentions a refund/discount/credit: only if it's written policy or the owner approved it.")
    placeholders = find_placeholders(raw, is_html)
    if placeholders:
        shown = "; ".join(f"line {n}: {w}" for n, w in placeholders[:10])
        more = f" (+{len(placeholders) - 10} more)" if len(placeholders) > 10 else ""
        issues.append(f"Placeholders to replace before this goes out: {shown}{more}")
    if kind == "social_x" and len(text.strip()) > 280:
        issues.append(f"{len(text.strip())} characters: X allows 280.")
    if kind == "landing":
        first = next((s for s in sentences if s.strip()), "")
        if len(first.split()) > 12:
            issues.append(f"Headline/first line is {len(first.split())} words; say what it is, for whom and the "
                          f"outcome in about 12.")

    lines.append(("Issues:\n- " + "\n- ".join(issues)) if issues else "No issues found.")
    return "\n".join(lines)
