"""The playwright connector (Phase 1, docs/phases/phase-1-connect.md section 7).

Playwright's MCP server, started with npx over stdio, gives the agents a real browser. --isolated keeps its
profile separate from the owner's logins; --headless keeps it off the screen. The flag names and the tool list were
read from the Playwright MCP README on Oct 14, 2026 and change between releases, so re-check them when upgrading.

Rules Holdco adds on top of the risk gate:
- browser_evaluate and browser_run_code_unsafe are blocked: never listed, always refused.
- browser_navigate to this PC (127.0.0.1, localhost) or the home network is refused for find_tool-loaded calls;
  only browser_check opens the local preview. Non-http(s) addresses (file:, data:, javascript:) are refused too.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import shlex
import shutil
import socket
import threading
import urllib.parse

DEFAULT_COMMAND = "npx -y @playwright/mcp@latest --headless --isolated --browser chromium"
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
    """Why a host is off limits for the agents' browser, or None when it is a public address."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return "That address has no host."
    if h == "localhost" or h.endswith(".localhost"):
        return f"This PC's addresses are off limits here: {OWN_SITE}."
    try:
        ips = [ipaddress.ip_address(h)]
    except ValueError:
        from . import web
        if web.is_relay(h):
            return f"{h} is a relay that fetches other sites; Holdco blocks relays."
        try:
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(h, None)]
        except (socket.gaierror, UnicodeError, ValueError):
            return None   # the browser reports a name that does not resolve on its own
    for ip in ips:
        if ip.is_loopback or ip.is_unspecified:
            return f"This PC's addresses are off limits here: {OWN_SITE}."
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
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
    "launch": launch,
    "mcp": {"command": _command, "args": _args, "env": {}, "classify": CLASSIFY, "allow": ALLOW,
            "blocked": list(BLOCKED)},
    "refuse_call": refuse_call,
}
