"""browser_check (Phase 1, docs/phases/phase-1-connect.md section 8): render a page with Playwright MCP when it
is set up, otherwise a labelled static check. Scaffold: the tool is registered with a placeholder body."""
from __future__ import annotations

from .tools import Ctx, S, tool


@tool("browser_check", "Open a page of the company site (a path in site/) or a public URL and check it: status, "
      "title, console errors, links, forms and screenshots.",
      {"path_or_url": S("A path in site/, e.g. site/index.html, or an http(s) URL"),
       "mobile": {"type": "boolean", "description": "Also check a phone-sized screen (default true)"}},
      ("path_or_url",), risk="read")
def browser_check(ctx: Ctx, path_or_url: str, mobile: bool = True) -> str:
    return "ERROR: browser_check is not available yet."
