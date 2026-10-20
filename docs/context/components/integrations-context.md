# Integrations: connectors, MCP, email, web, browser

## Purpose
Connects companies to the outside world with setup cards, health checks, metric pipes and risk-tagged tools, and
lets other tools (Claude Desktop, Claude Code) drive Holdco through MCP.

## Key files
- `holdco/connectors.py` (Phase 1): the framework. `MODULES` lists `conn_*.py`; each exports `DEFINITIONS` (label,
  category, kind `api`/`mcp-stdio`/`mcp-http`/`builtin`, scope global or company, setup card fields, `health`,
  `tools` or `mcp`, `metrics`, `test_mode`, optional `available: False` with a `reason`). Instance CRUD and the
  dashboard API, health checks (hourly and on save, Doctor rows, one phone alert a day), metric polls writing
  `metrics` rows with `source = <connector key>`, `find_tool` (loads a service's tools into a session on demand),
  `call()` through `actions.gate` with `policy_action` from the risk tag, `is_external()` (taint),
  `prepare_call` (per-connector request preparation, e.g. Porkbun's price) and `kind_for` (per-instance transport).
- Connectors: `conn_stripe` (payment links, payments, refunds that ask, revenue and MRR metrics, the hosted MCP
  server as `stripe_mcp`), `conn_resend` (email), `conn_playwright` (a real browser through Playwright's MCP server,
  pinned version, `external`), `conn_buffer` (beta), `conn_brave` (search), Phase 4: `conn_porkbun` (domains, DNS,
  purchases), `conn_cloudflare` (Workers static assets deploys), `conn_github` (GitHub's MCP server, role and repo
  allowlist, issue text external), `conn_plausible` (`plausible` and `umami` analytics metrics), `conn_forms`
  (Netlify forms or a Worker; signups become contacts), `conn_n8n` (webhook triggers; `n8n_mcp`), `conn_later`
  (ComfyUI, ads, accounting: not yet available).
- `holdco/mcp.py` (Phase 1): MCP client for both protocol generations (2026-07-28 stateless with `server/discover`
  and namespaced `_meta`; 2025-11-25 `initialize`), stdio and Streamable HTTP (JSON or SSE).
- `holdco/oauth.py` (Phase 1): OAuth with PKCE S256, discovery, dynamic registration, refresh; tokens per issuer.
- `holdco/mcp_server.py` (Phase 4): Holdco as an MCP server. `handle(message, client)` for both generations;
  eight tools (`list_companies`, `get_company_brain`, `list_inbox`, `decide`, `create_task`, `get_metrics`,
  `latest_brief`, `doctor`); `POST /mcp` (batches up to 100, `GET` 405, `DELETE` 200, Origin check, Basic auth);
  `python -m holdco.mcp_server` stdio proxy to the running dashboard.
- `holdco/emailer.py`: SMTP or Resend out, IMAP in; `_route_inbound` (stop requests suppress; optional small-model
  triage), `outbound()` (how mail would go out right now).
- `holdco/web.py`: search chain (Brave, SearXNG, the `ddgs` package, DuckDuckGo HTML; failing providers skipped for
  ten minutes), `fetch_page`, `check_public_url` (blocks local and private addresses unless `HOLDCO_ALLOW_LOCAL`).
- `holdco/browser.py` (Phase 1): `browser_check` through Playwright, or a labelled static check.

## Dependencies
- `vault` (secret fields as `connector.<instance id>.<field>`), `actions` (gate), `taint`, `doctor`, `notify`,
  `db`, `brain` (contacts from forms), `verify` (suppression).

## Integration points
- Agents see connected tools only through `find_tool`; "connectors down right now" is in every prompt.
- Metric polls feed the brain digest, the briefs and the portfolio scorecard (connector sources score; manual ones
  do not).
- `/mcp` and the stdio proxy reach `actions.decide` (decided_by `mcp`, audit `mcp:<client>`) and the task route's
  validation (`created_by "owner (mcp)"`).

## Patterns and rules
- A connector ships with a fake (`tests/fake_<name>.py`) and tests through it; test-only API bases (`api_base`,
  hidden fields) are accepted only while `HOLDCO_ALLOW_LOCAL` is set.
- Vendor endpoint paths not confirmed against live documentation are named constants marked "verify".
- Never log or return secrets; connector errors shown to agents are redacted.
