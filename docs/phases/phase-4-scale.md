# Phase 4: Scale

Phase 4 opens Holdco to other tools and spreads the GPU across the portfolio by results. Holdco becomes an MCP
server so Claude Desktop, Claude Code or any MCP client can read the portfolio, inbox, brain and metrics and
create tasks or decide approvals without the dashboard. A capital allocation score, computed from goal
progress, revenue, GPU hours, money spent and days since the last real-world result, drives each company's share
of concurrency and a monthly portfolio review. The remaining essential connectors ship: Porkbun, Cloudflare,
GitHub, Plausible and Umami, forms, and n8n; the later tier (ComfyUI, ads, accounting) appears as documented
"not yet available" cards. This is the implementation spec; the choices behind the connectors are in
[connectors.md](../connectors.md) and the owner's side in [owner-experience.md](../owner-experience.md).

Status: Planned. Branch `claude/phase-4-scale`, PR based on Phase 3. Depends on Phase 1 (`mcp.py`,
`connectors.py`, `vault.py`, `oauth.py`, risk tags), Phase 2 (`brain.py`, `briefs`, decisions, `rhythms.py`) and
Phase 3 (`trust.py`, taint, scheduler priorities). See [architecture.md](../architecture.md) for where the new
modules sit and [testing.md](../testing.md) for the fakes.

## Scope

1. Holdco as an MCP server (`mcp_server.py`): Streamable HTTP at `/mcp` and stdio, both protocol generations,
   eight tools, client configuration examples.
2. Portfolio capital allocation (`portfolio.py`): the scorecard, the score, the scheduler share, the monthly
   portfolio review, the Portfolio page.
3. Connectors: `conn_porkbun.py`, `conn_cloudflare.py`, `conn_github.py`, `conn_plausible.py` (with Umami),
   `conn_forms.py`, `conn_n8n.py`; later-tier definitions `comfyui`, `ads`, `accounting`.

Not in this phase: ComfyUI image generation, ad platforms, accounting (their cards explain why).

## Baseline: what exists today

| Area | Today (main branch) | After earlier phases | After Phase 4 |
|---|---|---|---|
| Outside access to Holdco | The JSON API under `/api/` (Basic auth with `HOLDCO_PASSWORD`, `X-Holdco` header on writes) | Telegram buttons (Phase 0) | An MCP endpoint at `/mcp` and a stdio entry point |
| Portfolio page | `Portfolio()` in `app.js`: company rows with agents working, open tasks, done today | Pinned daily brief (Phase 2) | A scorecard table with the allocation score and the monthly review |
| Concurrency | `Scheduler.tick` dispatches by `priority, id` across companies up to `max_concurrent_runs` | Priority classes (Phase 3) | Company share by score inside classes 3 to 6; one guaranteed run per cycle |
| Deploys | `actions._deploy_netlify` zips `site/` to Netlify | Phase 1 connectors framework | Cloudflare Workers static assets as an alternative target |
| Domains, repos, analytics, forms, automation | None | Playwright, Stripe, Resend, Buffer, Brave (Phase 1) | Porkbun, GitHub, Plausible or Umami, forms, n8n |

## Design

### 1. Holdco as an MCP server (`mcp_server.py`)

Behaviour. One request handler, `mcp_server.handle(message: dict, client: ClientInfo) -> dict | None`, serves
both transports. It speaks both protocol generations the Phase 1 client speaks: the current stateless generation
(MCP 2026-07-28: `server/discover`, namespaced `_meta` keys in each request, `resultType` in each result) and the
earlier one (`initialize`, `notifications/initialized`). Method table:

| Method | Reply |
|---|---|
| `server/discover` | `protocolVersions ["2026-07-28"]`, `capabilities {"tools": {}}`, `serverInfo {"name": "holdco", "version": <holdco.__version__>}` |
| any new-generation request | Reads `_meta["io.modelcontextprotocol/protocolVersion"]` (and `clientCapabilities`, `clientInfo` when present); answers JSON-RPC error `-32022` when the version is not `2026-07-28`; adds `_meta["io.modelcontextprotocol/serverInfo"]` and `resultType: "complete"` to every result (Holdco never needs a further round trip, so it never answers `input_required`) |
| `initialize` (old generation) | `protocolVersion` = the client's when it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, else `2025-11-25`; same capabilities and serverInfo |
| `notifications/initialized` (old generation) | No reply |
| `ping` (old generation only; removed in 2026-07-28) | `{}` |
| `tools/list` | The eight tool definitions with JSON schemas |
| `tools/call` | `{"content": [{"type": "text", "text": ...}], "structuredContent": {...}, "isError": bool}` |
| Anything else | JSON-RPC error `-32601` |

Malformed JSON is `-32700`; a missing `method` is `-32600`; a tool error is `isError: true` with the message, never
a JSON-RPC error. The field names of the current generation are the ones the brief verified against the MCP
2026-07-28 changelog on Oct 14, 2026 and the ones Phase 1's client sends; `tools/list` results also carry `ttlMs`
and `cacheScope` (Holdco answers a 60 s ttl, scope per client).

Tools. Every call runs as the owner. Writes are audited (`audit.approved_by = "mcp:<clientInfo.name>"`,
`proposed_by` the agent as usual). Secrets are never returned.

| Tool | Arguments | Risk | What it does |
|---|---|---|---|
| `list_companies` | none | read | Companies with status, stage, mission, `stats` (as `/api/state`), active goals with current and target, pending approvals count |
| `get_company_brain` | `company_id` (int) | read | `brain.digest(company)` plus every brain section as structured content |
| `list_inbox` | `company_id` (int, optional) | read | Pending `approvals`: requests, questions and decisions with payload (secrets masked), options, recommendation, deadline, taint reason |
| `decide` | `approval_id` (int), `decision` (`approve`, `reject`, `answer`), `feedback` (str, optional), `answer` (str, for questions and decisions) | write (publish, contact or spend depending on the row) | Calls `actions.decide` with `decided_by = "mcp"`; the kill switch refusal and every gate rule apply as in the dashboard |
| `create_task` | `company_id` (int), `title`, `description`, `assign_to` (role), `deliverable`, `acceptance`, `goal_id` (int), `priority` (1-3), `due_days` (int), `checks` | write | Same validation as `POST /api/companies/<id>/tasks`, `created_by = "owner (mcp)"` |
| `get_metrics` | `company_id` (int), `name` (optional), `days` (int, default 30) | read | `metrics` rows with `source`, newest first, up to 500 |
| `latest_brief` | `company_id` (int, optional; omitted means the portfolio brief), `kind` (optional) | read | The newest `briefs` row (body and data) |
| `doctor` | none | read | `doctor.LAST` rows and the summary |

Transports.

- Streamable HTTP: `POST /mcp` on the dashboard port. The body is one JSON-RPC message or a batch; the response
  is `application/json` (Holdco answers synchronously and never opens a server-to-client stream, so a client's
  `Accept: text/event-stream` is accepted but unused). `/mcp` accepts, and does not require, the new generation's
  `Mcp-Method` and `Mcp-Name` headers; when present they must match the body's method (else `-32600`). `GET /mcp`
  returns 405; `DELETE /mcp` returns 200 (there is no session state; `Mcp-Session-Id` is neither issued nor
  required, in either generation Holdco serves). The `X-Holdco` CSRF header is not
  required on `/mcp`; instead the handler rejects any request carrying an `Origin` header that is not the
  dashboard's own origin (a browser page cannot reach it, a native client has no `Origin`). Authentication: when
  `HOLDCO_PASSWORD` is set, Basic auth exactly as the dashboard (`Handler._auth_ok`); without a password the
  endpoint is reachable only because `run.py` already refuses to bind beyond 127.0.0.1 without one.
- stdio: `python -m holdco.mcp_server` reads newline-delimited JSON-RPC from stdin and writes replies to stdout
  (logs to stderr). It is a thin proxy: `server/discover`, `initialize` and `ping` are answered locally; every
  other message is forwarded to `http://127.0.0.1:<HOLDCO_PORT>/mcp` with the password from `HOLDCO_PASSWORD`,
  so there is one process writing the database and one set of audit rows. When the dashboard is not running the
  reply is an `isError` text: "Holdco is not running. Start it (start_windows.bat or ./start_linux.sh) and try
  again." (The proxy design is chosen here; the brief fixes the entry point, not the mechanism.)

Client configuration. Claude Desktop reads a JSON configuration file with an `mcpServers` object; add an entry
that starts the stdio entry point (verify the file location and shape against the vendor docs before
connecting):

```
{"mcpServers": {"holdco": {"command": "python",
                            "args": ["-m", "holdco.mcp_server"],
                            "env": {"PYTHONPATH": "C:\\path\\to\\Holdco",
                                    "HOLDCO_PORT": "8420", "HOLDCO_PASSWORD": "your-password-if-set"}}}}
```

`PYTHONPATH` makes `holdco.mcp_server` importable from any working directory, so the entry needs nothing
beyond `command`, `args` and `env`.

Claude Code: register the same stdio command with its `mcp add` command, or register the HTTP transport with the
URL `http://127.0.0.1:8420/mcp` and a Basic `Authorization` header when a password is set (verify the exact
command flags against the vendor docs before connecting). Both examples go in the README with a screenshot of
the "holdco" tools appearing in the client.

Code touch points: `server.Handler._handle` (route `/mcp` before the `/api/` branch, Origin check, no `X-Holdco`),
`server.py` imports `mcp_server`, `actions.decide` (`decided_by` value), `audit` writers, `README.md`.

### 2. Portfolio capital allocation (`portfolio.py`)

Scorecard. `portfolio.scorecard() -> list[dict]`, one row per company with status `active` or `paused`:

| Column | Source |
|---|---|
| `goal_progress` | Mean over active goals of `min(1, current / target)` where `current` is the latest `metrics` value named `goal.metric`; 0 with no goals |
| `revenue_30d` | Latest `stripe.revenue_usd_30d` |
| `mrr` | Latest `stripe.mrr_usd` |
| `gpu_hours_week` | Sum of run durations (`runs.finished_at - started_at`, running runs to now) started in the last 7 days, in hours |
| `money_spent_30d` | Sum of `payload.amount_usd` over `approvals` of action `spend_money` with status `done` in 30 days |
| `days_since_result` | Days since the newest of: a `metrics` row with `source != 'manual'` whose value differs from the previous row of the same name; an inbound email that references an outbound message id; a Stripe payment (`stripe.payments_30d` increasing) |
| `age_days` | Since `companies.created_at` |
| `score`, `share` | Below |

Score (a proposal, as the brief asks; the signals are the brief's). Each signal is scaled within the portfolio:

- `progress` = `goal_progress` (already 0-1).
- `traction` = `log1p(revenue_30d) / max over active companies of log1p(revenue_30d)`; 0 for every company when
  nobody has revenue.
- `freshness` = `max(0, 1 - days_since_result / 14)`; a company with no real-world result ever has 0.
- `score = 0.4 * progress + 0.4 * traction + 0.2 * freshness`.
- Seed period: a company younger than 14 days gets `max(score, 0.5)` so a new company is not starved before it
  can have results.
- Boost: a company the owner chose to "Scale" in the last portfolio review has `score * 1.5` for 30 days
  (company setting `allocation_boost_until`).
- `share = score / sum(score)` over active companies; paused companies have share 0.

`gpu_hours_week` and `money_spent_30d` are shown but not in the score: spending more GPU is not a merit, and
money only moves when the owner says so. The weights live in `portfolio.WEIGHTS` so a later phase can tune them
from Bench-style evidence.

Scheduler effect. Phase 3's class order stays. For classes 1 and 2 (owner follow-ups, due tasks) the company
does not matter. For classes 3 to 6, candidates are ordered by company deficit, then `priority, id`: `deficit =
share * runs_started_in_last_60_min_total - runs_started_in_last_60_min_for_company`. Nobody starves: a company
with a `todo` task and no run started in its `cycle_minutes` gets the next free slot regardless of deficit. The
global setting `allocation_mode` (`weighted` default, `equal` for Phase 3 behaviour) switches it off.

Monthly portfolio review. `portfolio.monthly_review()` runs from `Scheduler._housekeeping` when the oldest
company is at least 30 days old and no `briefs` row with `company_id NULL` and `kind = 'portfolio'` exists in
the last 28 days. It is built in code (with the Phase 3 `summary` job for the narrative when a small model is
set): the scorecard table, each company's recommendation by rule, and one `decision` per company:

| Rule | Recommendation |
|---|---|
| `score >= 0.6` and `days_since_result <= 7` | Scale |
| `age_days > 60`, `revenue_30d == 0`, `days_since_result > 45` | Shut down |
| Otherwise | Hold |

The decision: "PawPlan: scale, hold or shut down?" options "Scale (more GPU share for 30 days)", "Hold",
"Shut down (pause the company)"; recommended per the rule; `reversible` true unless the recommendation is Shut
down, in which case the decision waits for the owner; deadline 7 days. Applying the answer: Scale sets
`allocation_boost_until`; Hold does nothing; Shut down sets the company to `paused` with a note (Holdco never
deletes a company on its own). The CEO's monthly strategy check (Phase 2) stays separate: it is the company's
view, this is the board's.

Portfolio page and API. `GET /api/portfolio` returns `scorecard`, `latest_brief` (daily portfolio brief),
`latest_review` (the portfolio brief). The Portfolio page adds a scorecard table above the company rows: name,
goal progress (bar), revenue 30 d, MRR, GPU hours this week, money spent, days since result, score, share; sorted
by score; the pinned daily brief stays at the top; a "Monthly review" panel shows the latest review and its open
decisions. Companies with no goals show "no goals: set them in the Brain tab".

Code touch points: `scheduler.Scheduler.tick` (deficit ordering, starvation guard), `scheduler._housekeeping`
(`monthly_review`), `actions.decide` (applying Scale and Shut down answers), `config.DEFAULT_SETTINGS`
(`allocation_mode`), `config.DEFAULT_COMPANY_SETTINGS` (`allocation_boost_until`), `static/app.js` `Portfolio`.

### 3. Connectors

Every connector is a `connectors.DEFINITIONS` entry (Phase 1 shape: `label`, `category`, `kind`, `scope`, setup
card, `health`, `metrics`, `tools` or `mcp`, `test_mode`). Secret fields go to the vault as
`connector.<id>.<field>`; health checks run on save and hourly; failing instances show on the Doctor page and in
every agent's "Connectors that are down right now" list. External endpoint names and payloads are described in
words; the implementer takes them from the vendor's current documentation (verify against the vendor docs before
connecting).

#### porkbun (`conn_porkbun.py`)

| Item | Value |
|---|---|
| Kind, scope | `api`, global (API keys are account-level); purchases are attributed to the requesting company |
| Fields | `api_key` (secret), `secret_key` (secret), `dry_run` (bool, default true). The mode is not a field: sandbox keys start with `pk1_sb_`, so the card derives and shows it from the key prefix (like Stripe's mode) and confirms it from the `"sandbox": true` flag Porkbun puts in its responses |
| Setup card | Where to create API keys in the Porkbun account; create the key without purchase rights unless you intend to buy; start with sandbox keys; the plan notes Porkbun's own default agent spend cap of $100 a month |
| Health | The API's authenticated ping-style call (base `https://api.porkbun.com/api/json/v3`; the keys go in the `X-API-Key` and `X-Secret-API-Key` headers; the endpoint path is read from the reference) |
| Test mode | A `pk1_sb_` key or `dry_run` true shows "test mode" on the card. `register_domain` and `set_dns_record` send an `Idempotency-Key` header (the approval id; Porkbun replays a repeat within 24 h instead of acting twice) and `dryRun: true` while `dry_run` is on, which returns the would-be result without buying or changing anything |
| Tools | `porkbun__check_domain(domain)` [read] availability and price; `porkbun__list_domains()` [read]; `porkbun__dns_records(domain)` [read]; `porkbun__set_dns_record(domain, type, name, content, ttl)` [write, `connector_write`]; `porkbun__register_domain(domain, years=1)` [spend + irreversible: always `approve`, locked like `spend_money`; the Inbox card shows the price and "This buys a domain and cannot be undone"] |
| What agents can do | Check names and prices during founding and branding; manage DNS for the site and email domain; propose a purchase |

`request_purchase` stays the way an agent asks for anything else; a domain purchase through Porkbun is the one
case where approval executes the purchase, which is why the tag is both `spend` and `irreversible`.

#### cloudflare (`conn_cloudflare.py`)

| Item | Value |
|---|---|
| Kind, scope | `api`, company |
| Fields | `api_token` (secret, scoped to Workers), `account_id`, `worker_name` (default `<slug>-site`), `custom_domain` (optional) |
| Setup card | Create an API token limited to Workers; where to find the account id; the plan's note that Workers static assets are the recommended target over Pages |
| Health | The token verification call |
| Test mode | None (a deploy is a deploy); deploys stay `approve` until the trust ladder promotes `deploy_site` |
| Executor | `actions.execute("deploy_site")` picks the target from the company setting `site_provider` (`auto`, `netlify`, `cloudflare`; `auto` means Cloudflare when configured, else Netlify as today). The Cloudflare path uploads `site/` as the Worker's static assets through the API in words: create or update the Worker with an assets manifest, upload the files, attach the custom domain when set. `site_url` is updated as the Netlify path does |
| Tools | `cloudflare__deploy_status()` [read]: last deploy time and URL |
| What agents can do | Publish the site; nothing else |

#### github (`conn_github.py`)

| Item | Value |
|---|---|
| Kind, scope | `mcp-http` (the official hosted server with a token) or `mcp-stdio` (the official Docker image, started with the read-only flag named in the plan); global |
| Fields | `token` (secret: a fine-grained personal access token limited to the company's repositories), `mode` (`hosted`, `docker`), `repos` (list the agents may touch) |
| Setup card | Create the token with contents, issues and pull requests permissions on the chosen repositories only |
| Health | Connect and `tools/list` (Phase 1 client) |
| Classification map | `get_*`, `list_*`, `search_*` read; `create_issue`, `update_issue`, `add_issue_comment`, `create_pull_request`, `create_branch`, `push_files` write; `merge_pull_request`, `delete_*` irreversible; anything else `connector_publish` (asks) |
| Role allowlist | Every role: read tools only. `engineering`: read plus the write tools (tagged `write`, so `connector_write` policy and trust apply). Nobody gets `irreversible` tools without the owner |
| Taint | Issue and PR text is external content (`external=True`), so reading it taints the session (Phase 3) |
| What agents can do | Read repos and issues for research; engineering files issues and opens PRs after approval; merges and deletions always ask |

#### plausible and umami (`conn_plausible.py`)

| Item | Value |
|---|---|
| Kind, scope | `api`, company; two definitions, `plausible` and `umami`, sharing the module |
| Fields | plausible: `api_key` (secret), `site_id` (the site domain), `base_url` (default the cloud host, overridable for self-hosted); umami: `base_url`, `api_token` (secret) or `username` plus `password` (secret), `website_id` |
| Setup card | plausible: the plan notes the Stats API is a Business-plan feature on the cloud service, self-hosted has it; umami: where to create the token |
| Health | One stats query for yesterday |
| Metrics (polled hourly by `connectors.poll_metrics`) | `analytics.visitors`, `analytics.visits`, `analytics.conversions` (goal completions), each the last 7 days, note `7d`; stored with `source = plausible` or `umami` |
| Tools | `plausible__top_pages(days)` and `plausible__sources(days)`, or `umami__top_pages(days)` and `umami__sources(days)`, all [read]. The tool prefix is the connector key, as Phase 1 item 6 fixes it, so `connectors.call` resolves the instance; only the metric names share the `analytics.` prefix |
| What agents can do | Read traffic for the weekly review and SEO tasks; nothing is written |

#### forms (`conn_forms.py`)

| Item | Value |
|---|---|
| Kind, scope | `api`, company |
| Fields | `mode` (`netlify`, `worker`); netlify: reuses the company's Netlify token (vault) and `netlify_site_id`, plus `form_name`; worker: `endpoint_url`, `shared_secret` (secret) |
| Setup card | netlify: add `data-netlify="true"` and a `name` to the form (the audit already checks for it); worker: a template Worker in `docs/forms-worker.js` that stores submissions and serves them to a GET with the shared secret header |
| Health | List submissions (netlify) or GET the endpoint (worker) |
| Polling | Every hour: new submissions since the last seen id become `contacts` rows (`stage = lead`, `source = forms:<form name>`, `consent` from a `consent` field when the form has one, else empty and flagged, `email` deduplicated) and the metrics `forms.signups` (last 7 days) and `forms.signups_total`, `source = forms`. A contact in `suppression` is skipped |
| Tools | `forms__recent_signups(days)` [read] |
| What agents can do | See real signups as a metric and as CRM leads; sales follows up through the normal outreach gate (first message to a new contact always asks) |

#### n8n (`conn_n8n.py`)

| Item | Value |
|---|---|
| Kind, scope | `api` (webhook) or `mcp-http` (its MCP endpoint); global or company |
| Fields | `webhook_url`, `webhook_secret` (secret, sent as a header), optional `mcp_url` and `mcp_token` (secret) |
| Setup card | How to create a webhook workflow with a header check; the catch-all for services without a connector |
| Health | A POST to the webhook with `{"ping": true}` expecting a 2xx; for MCP, `tools/list` |
| Tools | `n8n__trigger(workflow, payload)` [publish by default, so `connector_publish` asks]; MCP tools classified by the instance's map, unclassified ones ask |
| What agents can do | Reach anything the owner wired up in n8n, with the owner's yes until trust promotes it |

#### Later tier

`comfyui`, `ads` and `accounting` are `DEFINITIONS` entries with `available: False` and a `reason` shown on the
connector card instead of a form:

| Key | Reason on the card |
|---|---|
| `comfyui` | "Local image generation shares the GPU with the language model. It will be scheduled in the quiet window once a company has traction." |
| `ads` | "Ad platforms spend money per hour. They come after revenue justifies a hard daily cap per company." |
| `accounting` | "QuickBooks or Xero sync comes once there are transactions to reconcile." |

`GET /api/connectors` returns them with `available: false`; `find_tool` ignores them.

Code touch points: `connectors.DEFINITIONS` (six entries plus three later-tier), `actions.execute`
(`deploy_site` target selection), `config.DEFAULT_COMPANY_SETTINGS` (`site_provider`), `connectors.poll_metrics`
(analytics and forms), `brain.py` (contacts from forms), `static/app.js` (connector cards; "not yet available"
state; Cloudflare in the deploy card).

## Files

New: `holdco/mcp_server.py`, `holdco/portfolio.py`, `holdco/conn_porkbun.py`, `holdco/conn_cloudflare.py`,
`holdco/conn_github.py`, `holdco/conn_plausible.py`, `holdco/conn_forms.py`, `holdco/conn_n8n.py`,
`docs/forms-worker.js`, `tests/test_phase4.py`, `tests/fake_porkbun.py`, `tests/fake_cloudflare.py`,
`tests/fake_plausible.py`, `tests/fake_forms.py`, `tests/fake_n8n.py`, `tests/mcp_client.py` (a minimal client
built on `holdco.mcp.Client` for driving `/mcp` and the stdio proxy).

Changed: `holdco/server.py` (`/mcp` routing, Origin check, auth), `holdco/scheduler.py` (deficit ordering,
`monthly_review`), `holdco/actions.py` (deploy target, review answers, `decided_by`), `holdco/connectors.py`
(definitions, `external` flags, later-tier state), `holdco/config.py` (`allocation_mode`, `site_provider`,
`allocation_boost_until`), `holdco/db.py` (nothing new: the MCP server and the scorecard read existing tables),
`holdco/static/app.js` (Portfolio scorecard and review, connector cards), `holdco/static/style.css`,
`tests/fake_mcp_server.py` (GitHub-shaped tool list for the github connector test), `README.md` (MCP client
setup, new connectors).

## Tests

Unit (`tests/test_phase4.py`):

| Test | Proves |
|---|---|
| `TestMcpServer.test_both_generations` | `server/discover` answers with `protocolVersions`, capabilities and `serverInfo`; a new-generation request with an unsupported `_meta` version gets `-32022`; every new-generation result carries `resultType: "complete"` and `serverInfo` in `_meta`; `initialize` answers the older generation; `ping` answers only after `initialize`; `tools/list` returns eight tools; unknown method is `-32601`; bad JSON is `-32700` |
| `TestMcpServer.test_tools_read` | `list_companies`, `get_company_brain`, `get_metrics`, `latest_brief`, `doctor`, `list_inbox` return the seeded data with secrets masked |
| `TestMcpServer.test_tools_write_audited` | `create_task` creates a task with `created_by = "owner (mcp)"`; `decide` approves a queued post and writes an `audit` row with `approved_by = "mcp:<client>"`; with the kill switch on `decide` returns `isError` with the Phase 0 message |
| `TestMcpServer.test_http_auth_and_origin` | Over the real server (e2e helper): without a password `/mcp` works from localhost; with `HOLDCO_PASSWORD` it needs Basic auth; a request with a foreign `Origin` is refused; `GET /mcp` is 405 |
| `TestMcpServer.test_stdio_proxy` | `python -m holdco.mcp_server` answers `server/discover` locally and forwards `tools/call` to the running server; with the server stopped it returns the "not running" error |
| `TestPortfolio.test_scorecard_columns` | Seeded runs, metrics (with sources), approvals and emails produce the expected columns, including `days_since_result` from a connector metric change and not from a manual one |
| `TestPortfolio.test_score_and_share` | Weights, the seed period floor, the boost, shares summing to 1, paused share 0 |
| `TestPortfolio.test_scheduler_share` | With two companies and one slot, the higher-share company gets more of 10 ticks; a starving company still gets one run per `cycle_minutes`; `allocation_mode = equal` restores Phase 3 ordering |
| `TestPortfolio.test_monthly_review` | The review brief is created once, recommendations follow the rules, a Shut down decision is irreversible, answering Scale sets the boost |
| `TestConnectors.test_porkbun` | Health with the header auth, the mode derived from a `pk1_sb_` key and from `"sandbox": true` in the reply, `check_domain` read runs freely, `register_domain` always queues as spend and shows the dry-run result on approval while `dry_run` is true, a second write with the same `Idempotency-Key` is not applied twice (fake Porkbun) |
| `TestConnectors.test_cloudflare_deploy` | With `site_provider = cloudflare`, approving `deploy_site` uploads `site/` to the fake Cloudflare and updates `site_url`; with `netlify` the old path runs |
| `TestConnectors.test_github_roles` | Through the fake MCP server with a GitHub-shaped tool list, research can load only read tools; engineering can load `create_issue` (tagged write, queued under `connector_write`); `merge_pull_request` is irreversible and asks; reading an issue taints the session |
| `TestConnectors.test_analytics_metrics` | The hourly poll stores `analytics.visitors`, `analytics.visits`, `analytics.conversions` with `source = plausible` (fake Plausible) and `umami` (fake Umami) |
| `TestConnectors.test_forms_to_contacts` | New submissions become contacts with source and consent, suppressed emails are skipped, `forms.signups` is recorded with `source = forms`; both modes |
| `TestConnectors.test_n8n_trigger` | `n8n__trigger` queues under `connector_publish`; approval posts to the fake webhook with the secret header; health ping |
| `TestConnectors.test_later_tier_cards` | `GET /api/connectors` lists `comfyui`, `ads`, `accounting` with `available: false` and a reason; `find_tool` never returns their tools |

End to end (`tests/test_e2e.py`): the Portfolio page assets load and `GET /api/portfolio` returns a scorecard for
the founded company; the MCP endpoint lists tools and creates a task that the scheduler dispatches to the fake
model; the fake Telegram receives the monthly review decision with option buttons.

Fakes: new `fake_porkbun.py`, `fake_cloudflare.py`, `fake_plausible.py` (also serves the Umami shape),
`fake_forms.py` (Netlify forms and the Worker endpoint), `fake_n8n.py`; `fake_mcp_server.py` from Phase 1 with a
GitHub tool list; `mcp_client.py` for the server tests. All are `ThreadingHTTPServer` scripts in the
`tests/fake_ollama.py` pattern and need no network.

## Gate and how the suite proves it

Gate: a week on the daily brief and the phone. Everything the owner needs to see or decide reaches Telegram and
can be answered from there; the Portfolio page and the brief carry the real numbers.

The week, as a checklist the owner ticks on a real portfolio with real connectors:

| Day | Check | Pass when |
|---|---|---|
| 1 | Morning: the portfolio brief arrives on the phone before you open the PC | One Telegram message with every company's numbers and "Decisions waiting: N" |
| 1 | Every decision and approval in the brief has buttons on the phone | Tapping a button answers it; the dashboard shows the same state within a minute |
| 2 | A payment in Stripe test mode shows in the evening brief with source `stripe` | No agent typed it; the Brain tab metric says `[stripe]` |
| 2 | A form signup becomes a contact and a metric | Contacts shows the lead with its source; `forms.signups` moved |
| 3 | A site deploy to Cloudflare or Netlify is approved from the phone | `site_url` opens; the brief says what shipped |
| 3 | An escalation (unplug the model server for 10 minutes) reaches the phone within 5 minutes with options | One question, one recommended answer, resolved by a button |
| 4 | A GitHub issue is filed by engineering after a phone approval; a merge request waits | The audit log shows the MCP tool call and who approved |
| 5 | The weekly board memo arrives | Its numbers match the Stripe and analytics dashboards; goal-less tasks were cancelled |
| 6 | From Claude Desktop or Claude Code: list the inbox and decide one item through the `holdco` MCP tools | The decision shows `decided_by = mcp` in the dashboard |
| 7 | The Portfolio page's scorecard matches what you saw during the week; the monthly review (forced with a date change) proposes scale, hold or shut down for each company | Each recommendation follows the rules; a Shut down waits for you |
| 1 to 7 | You opened the dashboard only to read, not to unblock anything | Count of "had to open the PC to fix something" is 0 |

What the suite proves: with the fake Telegram, every `brief`, `decision`, `question` and approval created in the
e2e run produces exactly one Telegram message with buttons where buttons apply, and pressing each button
resolves the row (Phase 0 tests extended by the Phase 2 and 4 rows); `TestPortfolio.test_scorecard_columns`
proves the Portfolio numbers come from `metrics` rows with a connector source, run durations and approvals, never
from text; the connector tests prove each new connector's health, metrics and risk tags. The checklist itself is
the gate: it needs a real week and real services, which no test can stand in for.

## How to try it on your PC

1. Check out `claude/phase-4-scale`, start Holdco, open the Portfolio page: the scorecard shows your companies
   with a score; set goals in a company's Brain tab and watch its progress bar fill.
2. Connectors (company Settings): add Plausible or Umami for your site; within an hour `analytics.visitors`
   appears in the Brain's metrics with its source. Add forms (Netlify mode) and submit your own site's form; the
   lead appears in Contacts.
3. Add Porkbun with sandbox keys and `dry_run` on; ask the CEO in Chat to "check whether pawplan.co is available";
   then ask it to register it: the request lands in your Inbox as a purchase, and approving it shows the dry-run
   result.
4. Add Cloudflare and set `site_provider` to cloudflare; approve a deploy; open `site_url`.
5. Add GitHub with a fine-grained token for one test repository; ask engineering to "file an issue summarising the
   QA findings"; approve it from Telegram.
6. Start Holdco with `HOLDCO_PASSWORD` set, add the `holdco` entry to Claude Desktop's configuration, restart it,
   and ask Claude "what is waiting in my Holdco inbox?"; then "approve request 12". Check the audit log panel.
7. Set `companies.created_at` back 31 days on one company (or wait) and watch the monthly review arrive with one
   decision per company on your phone.

## Open questions

1. The stdio entry point proxies to the running dashboard rather than opening the database itself. A second
   writer process would be simpler to start but would send emails and run approvals from a process the scheduler
   does not know about. Confirm the proxy.
2. The score weights (0.4, 0.4, 0.2), the 14-day seed floor of 0.5 and the 1.5 boost are proposals. The brief
   names the signals, not the numbers. They are constants in `portfolio.WEIGHTS` so the owner can change them
   after a month of data.
3. `register_domain` executes a purchase on approval, which is the first action in Holdco that moves money by
   itself. The plan's rule "money is always $0 unless you approve a purchase" is kept (approval is required and
   locked), but the README's line "Holdco never pays for anything" has to change to "never without your
   approval, and only through Porkbun".
4. Analytics metrics are stored as 7-day windows. A daily series (`analytics.visitors_1d`) would suit the evening
   brief's day-over-day deltas better; the brief names only the three metric names.
5. `/mcp` without a password trusts every local process. That matches the dashboard today; a per-client token
   for MCP is not in the brief.
