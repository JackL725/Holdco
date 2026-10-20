# ADR-001: Standard library only, Python 3.10+, no build step

Status: Accepted | Date: 2026-10-20 (records a rule held since the first version)

## Context
Holdco runs on the owner's own PC, mostly Windows, started by double-clicking `start_windows.bat`. The owner is not
a developer. Every dependency is an install step that can fail, a version to keep in sync and a supply-chain risk
on a machine that holds API keys and can send email and buy domains.

## Decision
- The Python code uses the standard library only: `http.server`, `sqlite3`, `urllib`, `json`, `threading`,
  `subprocess`, `smtplib`/`imaplib`, `html.parser`. MCP, OAuth, the vault's file backend, the search chain and the
  connectors are all written on top of it.
- It runs on Python 3.10, 3.12 and 3.13, on Windows and Linux. CI runs all six combinations on every push.
- The dashboard is plain HTML, CSS and JavaScript in `holdco/static/`, with no build step and no framework.
- Optional outside programs are used only through their own interfaces and only when present: Ollama (HTTP),
  Docker or Podman (the code sandbox), Node.js with `npx` (Playwright's MCP server). Without them Holdco still runs,
  and the Doctor page says what is missing.

## Alternatives considered
- Flask or FastAPI with a React dashboard: nicer to write, but needs pip, npm and a build, and breaks the
  double-click install.
- An MCP SDK, an OAuth library or `requests`: each would save code, but each adds an install and a version to track.
  The protocol code is small enough to own and test against the fakes.

## Consequences
- More code to own (`mcp.py`, `oauth.py`, `vault.py`, `web.py`), all covered by tests against fakes.
- Windows-only behaviour shows up only in CI: thread attribute names that collide with `threading.Thread`
  internals (`_handle`, `_stop`), CRLF in files, `resolve()` on vanishing temp files, a coarse monotonic clock
  (use `time.perf_counter` for short durations), slower runners (generous test timeouts).
- A pull request that adds a dependency is refused; propose a standard-library implementation instead.
