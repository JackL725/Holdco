"""The playwright connector (Phase 1, docs/phases/phase-1-connect.md section 7).

Playwright's MCP server, started with npx over stdio, gives the agents a real browser. --isolated keeps its
profile separate from the owner's logins; --headless keeps it off the screen. The flag names and the tool list were
read from the Playwright MCP README on Oct 14, 2026 and change between releases, so re-check them when upgrading.

Rules Holdco adds on top of the risk gate:
- browser_evaluate and browser_run_code_unsafe are blocked: never listed, always refused.
- browser_navigate to this PC (127.0.0.1, localhost, ::1, 2130706433, 0177.0.0.1) or the home network is refused
  for find_tool-loaded calls; only browser_check opens the local preview. Non-http(s) addresses (file:, data:,
  javascript:) are refused too.
- Where the page really ended up is checked after every call (check_result): a redirect, a link the page
  followed or a name that now resolves to this PC or the home network sends the browser to about:blank, and the
  agent gets an error instead of the page. HOLDCO_ALLOW_LOCAL (the test switch) does not open this.
- A click, a typed field or an upload is tied to the page it was asked for on (page_of, page_query): when the
  owner approves it and the browser is on another page by then, it is refused.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import threading
import urllib.parse

# Pinned, so an upgrade never arrives unannounced (0.0.83 was the latest release on Oct 15, 2026). The card's
# command field overrides it.
DEFAULT_COMMAND = "npx -y @playwright/mcp@0.0.83 --headless --isolated --browser chromium"
HEALTH_TIMEOUT = 120   # the first start downloads the package (and maybe a browser)

READ_TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
              "browser_console_messages", "browser_network_requests", "browser_resize", "browser_wait_for",
              "browser_find", "browser_tabs", "browser_close"]
# A click or a typed field on someone else's site may submit a form: the owner decides.
CONTACT_TOOLS = ["browser_click", "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option",
                 "browser_hover", "browser_drag", "browser_handle_dialog"]
PUBLISH_TOOLS = ["browser_file_upload"]
BLOCKED = ["browser_evaluate", "browser_run_code_unsafe"]
# Ordered (glob, tag); the first match wins. Anything else is unclassified, which asks the owner.
CLASSIFY = ([(n, "read") for n in READ_TOOLS] + [(n, "contact") for n in CONTACT_TOOLS]
            + [(n, "publish") for n in PUBLISH_TOOLS])
ALLOW = {"*": list(READ_TOOLS), "qa": ["*"], "engineering": ["*"], "research": ["*"], "design": ["*"],
         "marketing": ["*"]}
OWN_SITE = "use browser_check for your own site"


def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def launch(inst: dict | None) -> tuple[str, list[str]]:
    """(command, args) from the card's command override, or the default npx line."""
    line = str(_cfg(inst).get("command") or "").strip() or DEFAULT_COMMAND
    try:
        parts = shlex.split(line, posix=os.name != "nt")
    except ValueError:
        parts = line.split()
    parts = [p.strip('"') for p in parts] or DEFAULT_COMMAND.split()
    return parts[0], parts[1:]


def classify(tool: str) -> str | None:
    for glob, tag in CLASSIFY:
        if fnmatch.fnmatchcase(tool, glob):
            return tag
    return None


def _local_reason(host: str) -> str | None:
    """Why a host is off limits for the agents' browser, or None when it is a public address. Numbers are read the
    way the browser reads them (2130706433 and 0177.0.0.1 are 127.0.0.1, ::ffff:127.0.0.1 too); names are
    resolved. Uses web's checks, without the HOLDCO_ALLOW_LOCAL test exemption."""
    from . import web   # imported here: web is only needed for these checks
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return "That address has no host."
    if h == "localhost" or h.endswith(".localhost"):
        return f"This PC's addresses are off limits here: {OWN_SITE}."
    if web.is_relay(h):
        return f"{h} is a relay that fetches other sites; Holdco blocks relays."
    for ip in web.host_ips(h):   # [] for a name that does not resolve: the browser reports that on its own
        inner = getattr(ip, "ipv4_mapped", None) or ip
        if inner.is_loopback or inner.is_unspecified:
            return f"This PC's addresses are off limits here: {OWN_SITE}."
        if web.blocked_ip(ip):
            return "Local and private network addresses are blocked for agents."
    return None


def refuse_call(tool: str, args: dict | None) -> str | None:
    """Checked by connectors.call before the risk gate for find_tool-loaded calls. A text means: refuse with it."""
    if tool in BLOCKED:
        return f"{tool} is not available through Holdco"
    if tool == "browser_navigate":
        url = str((args or {}).get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return "Only http(s) pages can be opened in the browser."
        return _local_reason(parsed.hostname or "")
    return None


# ---------------------------------------------------------------- where the page ended up

_PAGE_URL = re.compile(r"Page URL:\s*(\S+)")
_CURRENT_TAB = re.compile(r"\(current\).*\((\S*)\)\s*$")   # "- 0: (current) [Title] (https://...)"
BLANK = "about:blank"
RESET_CALL = ("browser_navigate", {"url": BLANK})        # leaves a page that must not stay open
PAGE_QUERY = ("browser_tabs", {"action": "list"})        # names the page the browser is on right now


def page_urls(text: str) -> list[str]:
    """Every page address a result names: the "Page URL:" lines Playwright MCP adds after an action or a snapshot,
    and the current tab of a browser_tabs listing."""
    text = str(text or "")
    found = _PAGE_URL.findall(text)
    for line in text.splitlines():
        m = _CURRENT_TAB.search(line.strip())
        if m:
            found.append(m.group(1))
    return found


def page_of(text: str) -> str | None:
    """The page the browser is on according to a result (the last address it names), or None when it names none."""
    found = page_urls(text)
    return found[-1] if found else None


def landing_problem(url: str) -> str | None:
    """What to name when a page address is this PC, the home network or a file on this PC, else None. Pages
    without a host (about:blank, a browser error page) are fine. HOLDCO_ALLOW_LOCAL does not apply."""
    from . import web   # imported here: web is only needed for these checks
    parsed = urllib.parse.urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return "a file on this PC"
    if scheme not in ("http", "https", "ws", "wss", "ftp"):
        return None
    host = parsed.hostname or ""
    return web.private_host(host) if host else None


def check_result(tool: str, args: dict | None, text: str, allow=None) -> str | None:
    """After every result of this connector (the framework calls it; browser_check calls it after its own calls):
    an error text when a page the result names is this PC or the home network, else None. The caller then sends
    the browser to about:blank (RESET_CALL). allow(url) -> bool lets browser_check accept its own preview."""
    for url in page_urls(text):
        if allow is not None and allow(url):
            continue
        host = landing_problem(url)
        if host:
            return f"ERROR: the page went to a local or private address ({host}); Holdco blocks that"
    return None


def _list_with_deadline(inst: dict, seconds: float) -> list[dict]:
    """connectors.client(inst).list_tools() with an overall deadline (a hung start must not hang the check)."""
    from . import connectors   # imported here: connectors imports the conn_* modules
    box: dict = {}

    def work():
        try:
            box["tools"] = connectors.client(inst).list_tools()
        except BaseException as e:   # reported to the caller below
            box["error"] = e

    t = threading.Thread(target=work, daemon=True, name="playwright-health")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"the server did not list its tools within {int(seconds)} s")
    if "error" in box:
        raise box["error"]
    return box.get("tools") or []


def health(inst: dict) -> tuple[str, str]:
    command, _ = launch(inst)
    if shutil.which(command) is None:
        if os.path.basename(command).lower() in ("npx", "npx.cmd", "npx.exe"):
            return "warn", "npx not found: install Node.js"
        return "bad", f"{command} not found"
    try:
        listed = _list_with_deadline(inst, HEALTH_TIMEOUT)
    except Exception as e:
        return "bad", f"Playwright MCP failed: {str(e)[-400:] or type(e).__name__}"
    names = {str(t.get("name")) for t in listed if isinstance(t, dict)}
    if "browser_navigate" in names:
        return "ok", f"{len(names)} browser tools ready"
    return "bad", "The server started but has no browser_navigate tool (a different package or version?)"


def tools(inst: dict | None) -> list[dict]:
    return []   # an MCP connector: its tools come from the server's tools/list


_command, _args = DEFAULT_COMMAND.split()[0], DEFAULT_COMMAND.split()[1:]

DEFINITION = {
    "label": "Playwright browser",
    "category": "Browser",
    "kind": "mcp-stdio",
    "scope": "global",
    "health_timeout": HEALTH_TIMEOUT,
    "setup": {
        "where_to_get_key": ("No key. Install Node.js (it brings npx), then turn this card on and press Test. The "
                             "first start downloads Playwright and a browser, so allow a minute or two."),
        "permissions": [],
        "fields": [
            {"key": "command", "label": "Command", "secret": False,
             "hint": "Leave blank for the default; --isolated keeps the browser away from your logins",
             "placeholder": DEFAULT_COMMAND},
        ],
        "test_mode": "Not applicable. Clicking or typing on other sites always asks you first.",
        "docs_url": "https://github.com/microsoft/playwright-mcp",
        "agents_can": ["Check the company's own pages with browser_check: console errors, screenshots, a phone-"
                       "sized view, links and forms",
                       "Open public pages and read them (find_tool)",
                       "Click, type or upload on other sites only with your approval; running page scripts is "
                       "never allowed"],
    },
    "health": health,
    "tools": tools,
    "external": True,   # Phase 3: every page it shows belongs to someone else, so reading one taints the session
    "launch": launch,
    "mcp": {"command": _command, "args": _args, "env": {}, "classify": CLASSIFY, "allow": ALLOW,
            "blocked": list(BLOCKED)},
    "refuse_call": refuse_call,
    # Read by the framework (connectors.py): where every result's page ended up, how to leave a page that must not
    # stay open, and how to tell which page a click or a typed field was asked for on.
    "check_result": check_result,
    "reset_call": RESET_CALL,
    "page_of": page_of,
    "page_query": PAGE_QUERY,
}
