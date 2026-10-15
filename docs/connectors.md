# Connectors and MCP

Holdco's connector layer is how agents reach services outside the PC: email, payments, a browser, social
posting, search and, later, domains, hosting, code, analytics and forms. Today each integration is a hand-written
code path with its keys in the database. The plan replaces that with one framework: a connector definition per
service, an MCP client so any MCP server plugs in, a credential vault, a risk tag on every tool that drives the
approval gate, a health check on save and every hour, and metric pipes that pull real numbers into the company
brain. Nothing listens on a port: every inbound fact is polled. This document records both states, the code as it
is and the design as decided, so an engineer can build each phase from it.

Status: design. Telegram and the search chain are built in Phase 0 (see [phases/phase-0-stabilize.md](phases/phase-0-stabilize.md));
the framework, vault, MCP client, OAuth, risk tags, `find_tool` and the first five connectors in Phase 1
([phases/phase-1-connect.md](phases/phase-1-connect.md)); the remaining connectors in Phase 4
([phases/phase-4-scale.md](phases/phase-4-scale.md)). "Today" below describes the committed code at the start of Phase 0.

Related: [architecture.md](architecture.md) (modules and tables), [operating-model.md](operating-model.md) (the brain the
metrics land in), [reliability.md](reliability.md) (Doctor, watchdog), [safety.md](safety.md) (gate, taint, trust ladder,
sandbox), [owner-experience.md](owner-experience.md) (setup cards, Inbox, phone), [testing.md](testing.md) (fake services).

## Today

### Email: SMTP and IMAP (`holdco/emailer.py`)

Each company can have its own mailbox, set in the company's Settings tab. The fields live in
`config.DEFAULT_COMPANY_SETTINGS`: `smtp_host`, `smtp_port` (587), `smtp_user`, `smtp_password`, `smtp_from`,
`smtp_from_name`, `imap_host`, `imap_port` (993), `imap_user`, `imap_password`.

- Sending: `emailer.send()` builds an `EmailMessage` with a `Message-ID`, `In-Reply-To` and `References` when
  replying, uses `SMTP_SSL` on port 465 and STARTTLS otherwise, 30 s timeout, then inserts a row in `emails` with
  `direction='out'`. It is called only by the `send_email` executor (after the gate) and by the test-send route.
  If SMTP is not set up, it raises `EmailError` at execution time: the approval ends `failed` and can be retried
  from the Inbox after the owner adds the details (tested in `tests/test_unit.py`).
- Receiving: `emailer.poll_inbox()` logs in with `IMAP4_SSL`, selects `INBOX` read-only, searches by UID above the
  last stored UID (first run: the last 3 days), fetches at most 50 messages, stores them with `direction='in'`
  and `status='new'`, emits an `email` event and creates a priority-1 task for the support agent (or the CEO)
  to answer it. `scheduler._check_inbox` polls every active company every 5 minutes in a thread.
- Tools: `send_email` (checks the address, the per-company `daily_email_limit` counting sent plus pending, then
  calls the gate), `check_inbox` (tells the agent "The inbox isn't connected yet" when IMAP is blank),
  `mark_email_handled`.
- Routines whose preset says `requires: imap` or `smtp` do not start until the mailbox is configured.
- Health: none automatic. The company Settings tab has a test send (`POST /api/companies/<id>/emails/test`) and a
  manual inbox check (`POST /api/companies/<id>/emails/check`).

### Website deploys and the posting webhook (`holdco/actions.py`)

- `deploy_site` (action `deploy_site`, in the engineering belt) refuses without `site/index.html`, then goes through
  the gate. The executor `_deploy_netlify` zips `site/`, creates a Netlify site on the first deploy when
  `netlify_site_id` is blank (named `<slug>-<number>`), uploads the zip to the site's deploys endpoint with a
  120 s timeout, and stores `netlify_site_id` and `site_url` back into the company settings. Without a
  `netlify_token` the approval succeeds with "Approved, but no Netlify token is set" and the site stays on the
  local preview at `/preview/<slug>/`.
- `social_post` (action `social_post`, marketing and social belts) posts JSON `{type: "social_post", company,
  platform, content, link}` to the company's `webhook_url` (Zapier, Make, n8n, Discord) with a 30 s timeout. An
  HTTP 4xx or 5xx fails the approval. Without a webhook the result is a one-click X intent link for the owner.
- Health: none. A bad token or webhook is discovered on the first approved deploy or post.

### Raw HTTP: the `http_request` tool

Only the analyst preset has it. `GET` runs immediately through `web.fetch_page` (public URLs only, 8,000
characters of text). Any other method is gated as action `http_post` with payload `{method, url, body, headers}`;
the executor `_http` sends a JSON body plus the agent's headers with a 30 s timeout and returns the status and
the first 8,000 characters. The Inbox card shows method, URL and body. Agents have no access to any key, so this
tool can only reach services that need none.

### Search providers (`holdco/web.py`)

`web.search(query, n, settings)` reads the global settings `search_provider` (`duckduckgo` | `searxng` |
`brave`), `searxng_url` and `brave_api_key`:

| Provider | How | Notes today |
|---|---|---|
| SearXNG | GET `<searxng_url>/search?q=&format=json`, 25 s, no proxy, skips the public-URL check (it runs on your LAN) | Used only when selected and a URL is set |
| Brave | GET `https://api.search.brave.com/res/v1/web/search?q=&count=` with the `X-Subscription-Token` header | Used only when selected and a key is set |
| DuckDuckGo | The optional `ddgs` package if installed, else a form POST to `html.duckduckgo.com/html/` then `lite.duckduckgo.com/lite/` | The default and the fallback when the selected provider is not configured |

There is no fallback between providers and no bot-challenge detection. A DuckDuckGo challenge page comes back as
HTTP 202 with no result rows, so the agent sees "No results. Try different words." and nothing tells the owner.
A SearXNG or Brave error surfaces as "Search failed (...)". This is the failure from the first real run that the
Phase 0 search chain fixes (see "The search chain" below).

### Where keys live today, and why that changes

| Secret | Stored in | Shown by the API |
|---|---|---|
| `smtp_password`, `imap_password`, `netlify_token` | `companies.settings` JSON, plain text | Masked as `••••••••` by `server._public_company`; `PATCH /api/companies/<id>` ignores the mask so a saved secret is not overwritten |
| `brave_api_key`, `llm_api_key` | `settings` table, plain text | Returned in clear by `GET /api/settings` and rendered into password fields |
| `webhook_url` | `companies.settings` JSON | In clear (it is a URL, but a webhook URL is a capability) |

`check_public_url` (tested in `tests/test_unit.py`) is the only structural guard: agents cannot reach loopback,
private, link-local, reserved or multicast addresses, and only `http(s)` is allowed.

Why that changes: keys in `holdco.db` are readable by anything that reads the file, including a backup copied
elsewhere; a dozen connectors multiply the blast radius; and MCP servers need tokens passed at launch. Phase 1
moves every secret into the vault, leaves only a marker in the settings, and redacts known secret values from
events and transcripts.

## The framework

Planned (Phase 1): module `holdco/connectors.py` holds the registry `DEFINITIONS` (one entry per connector key),
the instance table, the health and metrics loops, and `connectors.call()` which executes a connector tool. Each
real connector lives in its own module (`conn_stripe.py`, `conn_resend.py`, `conn_playwright.py`,
`conn_buffer.py`, Phase 4 adds `conn_porkbun.py`, `conn_cloudflare.py`, `conn_github.py`, `conn_plausible.py`
with Umami, `conn_forms.py`, `conn_n8n.py`) and registers its definition on import.

### Connector definition

| Field | Meaning |
|---|---|
| `label`, `category` | Shown on the setup card and the Doctor page |
| `kind` | `api` (Holdco calls a REST or GraphQL API), `mcp-stdio` (Holdco launches a local MCP server), `mcp-http` (a hosted MCP server over Streamable HTTP), `builtin` (search, notifications: code that already exists in Holdco) |
| `scope` | `global` (one instance for the whole install, for example Playwright, Brave) or `company` (one per company, for example Stripe, Resend, Buffer) |
| `setup` | The setup card: where to get the key, which permissions to tick, `fields` as `[{key, label, secret, hint, placeholder}]`, a `test_mode` note, `docs_url`, and what agents will be able to do with it |
| `health(inst) -> (status, detail)` | The check that runs on save and hourly; `status` is `ok`, `warn` or `bad` like a Doctor row |
| `metrics(inst, company) -> [(name, value, note)]` | The metric pipe, polled hourly; names are `<connector>.<metric>` |
| `tools(inst)` | For `api` connectors: tool specs (name, description, parameters) each with a risk tag |
| `mcp` | For MCP connectors: `command`/`args`/`env` or `url`, a classification map `{tool name or glob: tag}`, a per-role allowlist of which roles may load which tools, and a `blocked` list of tool names that are never exposed to any role |
| `test_mode(inst) -> bool` | Whether the instance is in the service's sandbox or dry-run mode; shown on the setup card |

### Instances

Table `connectors`:

| Column | Meaning |
|---|---|
| `id` | Instance id; secrets are keyed by it |
| `company_id` | `NULL` for a global instance |
| `key` | The definition key (`stripe`, `playwright`, ...) |
| `enabled` | Owner toggle; a disabled instance is neither checked nor offered to agents |
| `config` | JSON of the non-secret fields only |
| `status`, `detail`, `last_check` | Result of the last health check |
| `created_at` | UTC string from `db.now()` |

Secret fields never enter `config`. Each is stored in the vault under `connector.<id>.<field>` and the API
returns it masked. Today's email, Netlify and webhook settings stay where they are in Phase 1 (they are covered by
the Phase 0 Doctor rows); `connectors` holds the new connectors.

### Setup card

The dashboard renders one card per definition from `setup`: the steps to get a key, the permissions to tick
(for example a Stripe key with the Agent tag, or a Porkbun key without purchase rights), the fields, a test
button (`POST /api/connectors/<id>/test`), the test-mode note, and the list of tools agents gain, each with its
risk tag. Where the cards live in the dashboard is described in [owner-experience.md](owner-experience.md).

### Health checks

Every instance's `health()` runs when it is saved (`POST /api/connectors` returns the result so the owner sees a
wrong key immediately) and hourly from the scheduler. Results go to `status`, `detail`, `last_check` and appear as
one Doctor row per instance (see [reliability.md](reliability.md)). A failing instance alerts the owner once per
day through `notify.send`. The Phase 0 watchdog pattern `connector_down` (a connector row `bad` on two consecutive
checks, once per connector per day) turns a persistent failure into one escalation question in the Inbox and on
the phone.

### What agents are told when a connector is down

Every agent's system prompt gets a section "Connectors that are down right now" listing each failing instance's
label and detail, so the CEO does not plan work that depends on it and an executor does not burn its steps on it.
Today's behaviour for email stays: routines that require SMTP or IMAP do not start until the mailbox works, and
a disabled or missing connector is simply not offered to `find_tool`.

### Metrics pipes

The scheduler calls `connectors.poll_metrics()` hourly. For every enabled instance with a `metrics()` function it
inserts rows into `metrics` with the new `source` column (added through `db.MIGRATIONS`; existing rows get
`manual`) and `name` set to `<connector>.<metric>`, for example `stripe.mrr_usd`, `email.replies_7d`,
`analytics.visitors`. Pipes insert directly; they do not go through `record_metric`, whose name normaliser would
turn the dot into an underscore. `record_metric` keeps writing `source='manual'`. The Phase 2 brain digest shows
the latest value per name with its source, and the weekly review uses only rows with a connector source
([operating-model.md](operating-model.md)).

### Polling only: no open ports

Everything inbound is pulled on a schedule: Stripe charges and subscriptions, form submissions, analytics, IMAP
mail, Telegram updates (long polling). Holdco never opens a port on the owner's PC or router. The plan allows
webhooks as an option through a Cloudflare remotely-managed tunnel; Quick Tunnels are for testing only and do not
support Server-Sent Events (per the plan's Cloudflare source; verify against the vendor docs before connecting).
No phase in the current roadmap builds the tunnel path.

### API routes

| Route | Phase | What it does |
|---|---|---|
| `GET /api/connectors` | 1 | Definitions (with setup cards and tool lists), instances (secrets masked) and statuses |
| `POST /api/connectors` | 1 | Create or update an instance; secrets go to the vault; runs the health check and returns it |
| `POST /api/connectors/<id>/test` | 1 | Run the health check now |
| `DELETE /api/connectors/<id>` | 1 | Remove the instance and its vault entries |
| `POST /api/connectors/<id>/oauth/start` | 1 | Start the OAuth flow; returns the URL to open |
| `GET /oauth/callback` | 1 | The redirect target, served by the dashboard on the same port |
| `GET /api/doctor`, `POST /api/doctor/run` | 0 | Doctor rows, including one per connector |
| `GET /api/audit?company_id=&limit=` | 0 | Append-only audit log of every outbound action, connector calls included |
| `POST /api/notify/test` | 0 | Send a test message to Telegram or ntfy |

## MCP client

Planned (Phase 1): `holdco/mcp.py`, standard library only. `Client(name, transport, command/args/env or
url/headers, auth=None)` with `connect()`, `list_tools()`, `call_tool(name, args)`, `text_of(result)` and
`close()`. `call_tool` returns `{"content": [...], "isError": bool, "structuredContent": ...}`. All messages are
JSON-RPC 2.0 with request ids. The fake server `tests/fake_mcp_server.py` runs as stdio and as HTTP and speaks
both protocol generations, so negotiation, `tools/list`, `tools/call`, SSE responses and the 401 path are all
tested without network.

### Transports and framing

- stdio: Holdco starts the server as a subprocess (`npx ...` or `docker ...`), writes newline-delimited JSON
  frames to its stdin and reads frames from stdout on a reader thread. stderr is captured and shown in error
  details. `env` values may be vault markers; they are resolved at launch and never written to a transcript.
- Streamable HTTP: every request is a POST to the server URL with `Accept: application/json, text/event-stream`.
  The response is either a JSON body or an SSE stream; in the SSE case the client reads `data:` lines until it
  finds the message with its request id. In the current generation every POST also carries the `Mcp-Method`
  and `Mcp-Name` headers (the JSON-RPC method, and the name the request targets, such as the tool name on
  `tools/call`), and there is no session id. In the older generation only, the client echoes `Mcp-Session-Id`
  on every later request when the server sets it and sends the `MCP-Protocol-Version` header with the agreed
  version. A stream that breaks before the reply is not resumed; the request is re-issued with a new id. Extra
  `headers` from the definition (for example `Authorization: Bearer <key>`) are added from the vault.

### Two protocol generations

The current MCP generation (specification 2026-07-28; facts verified Oct 14, 2026) is stateless: no
`initialize` handshake, no `notifications/initialized`, no `ping`, no session id. Every request carries
namespaced keys in `_meta`, every result carries the server's identity and a `resultType`, and servers must
answer `server/discover`. Many servers still speak 2025-11-25, so the client supports both. The probe order is
fixed:

1. Send `server/discover` with `_meta` = `{"io.modelcontextprotocol/protocolVersion": "2026-07-28",
   "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": {"name": "holdco",
   "version": <holdco version>}}`.
2. If it answers with a result, the client is in the current generation: `client.protocol` records
   `"2026-07-28"`, every later request carries those three `_meta` keys and, over HTTP, the `Mcp-Method` and
   `Mcp-Name` headers. The client reads `_meta["io.modelcontextprotocol/serverInfo"]` from results and checks
   `resultType`: missing or `"complete"` is a finished result; `"input_required"` means the server wants another
   round trip that Holdco cannot provide, so the agent gets `ERROR: the server needs input Holdco cannot
   provide`. A JSON-RPC error `-32022` (unsupported protocol version) raises `MCPError("unsupported protocol
   version")`.
3. If `server/discover` fails with JSON-RPC error `-32601` or `-32600`, or with an HTTP 4xx or 5xx (a 401 still
   raises `AuthRequired`), send `initialize` with `protocolVersion "2025-11-25"` and `clientInfo` naming holdco,
   then `notifications/initialized`, and accept the version the server returns (`2025-11-25`, `2025-06-18` or
   `2025-03-26`). `client.protocol` records it. Only in this generation does the client echo `Mcp-Session-Id`
   and send the `MCP-Protocol-Version` header.

### Timeouts

Connect (launch and negotiation): 30 s. One tool call: 120 s. A timeout is a tool error
returned to the agent as text starting with `ERROR:`; the health check marks the instance `bad` with the detail.

### How results become text

`text_of(result)` joins the text content blocks. Image and resource blocks are replaced by a short note of their
type and size, never embedded. `isError` results come back to the agent as `ERROR: <text>`. The usual
`tools.MAX_RESULT` truncation (7,000 characters) applies after that. From Phase 3, content from an external
site read through a browser or connector is fenced as untrusted and taints the run ([safety.md](safety.md)).

### 401 and the OAuth path

An HTTP 401 with a `WWW-Authenticate` header raises `AuthRequired(resource_metadata_url)`. During a health check
or save, the check fails with a detail saying a sign-in is needed and the setup card offers the sign-in button,
which calls `POST /api/connectors/<id>/oauth/start`. During an agent call it is a tool error that names the
connector so the agent asks the owner instead of retrying.

## OAuth

Planned (Phase 1): `holdco/oauth.py`. Authorization code with PKCE (S256) and a `state` value. The redirect URI
is `http://127.0.0.1:<dashboard port>/oauth/callback`, served by the dashboard itself, so no extra port opens.

Discovery, in order:

1. Protected resource metadata (RFC 9728): from the `resource_metadata` value in `WWW-Authenticate`, else from
   `/.well-known/oauth-protected-resource` on the server.
2. Authorization server metadata (RFC 8414): `/.well-known/oauth-authorization-server`, falling back to OpenID
   `/.well-known/openid-configuration`.
3. Dynamic client registration (RFC 7591; deprecated by the MCP specification in favour of Client ID Metadata
   Documents, but still accepted) when the server offers it; otherwise the `client_id` from the connector's
   config.

Tokens (access, refresh, expiry, issuer, resource) are stored in the vault under `oauth.<connector id>`. A token
is only ever sent to the resource of the issuer that minted it; it is never reused with another authorization
server, as the MCP authorization specification requires. Refresh happens when less than 60 s remain. The flow is
started by `POST /api/connectors/<id>/oauth/start` (returns the URL to open) and finished by `GET /oauth/callback`,
which exchanges the code and stores the tokens; the test button then confirms access. When the callback carries
an `iss` parameter it must match the issuer recorded at start, else the flow is refused. `tests/fake_oauth.py`
plays the server.

## Risk tags and policies

Today: six policy actions (`send_email`, `social_post`, `deploy_site`, `http_post`, `run_code`, `spend_money`),
each `approve` | `auto` | `off`, global with per-company overrides, `spend_money` locked to `approve`
(`config.LOCKED_POLICIES`). `actions.gate()` applies them and deduplicates identical pending requests.

Planned (Phase 1): `config.RISK_TAGS = read, write, publish, contact, spend, irreversible`. Every tool carries one
tag, built-in tools through `@tool(..., risk=...)` in `tools.py`, connector tools through `tools()` or the MCP
classification map.

| Tag | Built-in tools | What it means |
|---|---|---|
| `read` | `web_search`, `fetch_url`, `read_file`, `list_files`, `recall`, `list_tasks`, `check_inbox`, `audit_page`, `check_contrast`, `review_text`, `browser_check`, `find_tool` | Looks at something; runs freely |
| `write` | `write_file`, `delete_file`, `create_task`, `update_task`, `remember`, `record_metric`, `update_charter`, `hire_agent`, `note_progress`, `mark_email_handled`, `report_to_owner`, `ask_owner`, `finish`, `run_code` | Changes Holdco's own state; free for built-ins (`run_code` still goes through the `run_code` policy) |
| `contact` | `send_email` | Messages a person |
| `publish` | `social_post`, `deploy_site`, `http_request` (non-GET) | Puts something where the public can see it |
| `spend` | `request_purchase` | Costs money |
| `irreversible` | (no built-in tool) | Cannot be undone: buying a domain, deleting a repo, a refund |

`run_code` is tagged `write` but keeps its own `run_code` policy. Built-in tools keep today's policy names
(`send_email` is still gated by the `send_email` policy); the tag is what the Inbox and the Phase 3 trust ladder
and taint rules key on.

Connector tools map to four new policy actions added to `config.POLICY_ACTIONS`:

| Tag on a connector tool | Policy consulted | Label in Settings |
|---|---|---|
| `read` | none, runs freely | |
| `write` | `connector_write` | Change data in a connected service |
| `publish` | `connector_publish` | Publish through a connected service |
| `contact` | `connector_contact` | Message people through a connected service |
| `spend` | `spend_money` | Spend money (locked to Ask me first) |
| `irreversible` | `irreversible` | Irreversible actions: domains, deletions, refunds (locked to Ask me first) |
| unclassified | `connector_publish` | An MCP tool Holdco has not classified asks by default |

`spend_money` and `irreversible` are in `LOCKED_POLICIES`: they are always `approve`, whatever the owner sets, and
the Phase 3 trust ladder never promotes them. The Inbox request card for a connector call shows the connector
label, the tool name, the tag as a badge and the arguments as JSON; the setup card shows the tag next to every
tool the connector adds.

## Small tool belts and `find_tool`

Why: a local model picks the right tool far less often as the list grows, and the presets already keep belts at
9 to 14 tools (`presets.py`). Dumping a Stripe server's dozens of tools and Playwright's twenty into every prompt
would undo that. So connector tools are never in the system prompt by default.

Planned (Phase 1):

- `find_tool(query)` is added to every role's belt (tag `read`). It searches the tools of every enabled connector
  the company can see (its own instances plus global ones) by name and description, returns up to 5 matches with
  one-line descriptions and tags, and loads the top 3 into the session as `ctx.loaded_tools`. `agents.run` merges
  loaded tools into `tool_specs` for the following steps, so the model sees them as ordinary tools.
- Loaded tools are named `<connector key>__<tool>`: `stripe__create_payment_link`, `playwright__browser_snapshot`,
  `github__create_issue`. The prefix tells the agent, the Inbox and the audit log which instance is involved.
- For MCP connectors the definition's per-role allowlist limits what a role may load (for example GitHub write
  tools only for engineering). `tools.execute` keeps refusing anything outside `ctx.allowed` plus `loaded_tools`.
- Execution: `tools.execute` routes a loaded tool to `connectors.call(ctx, name, args)`. The risk gate decides:
  `read` runs now; a gated tag goes through `actions.gate` as action `connector_call` with payload
  `{connector_id, tool, args, tag}`, which runs now under `auto`, queues under `approve`, or is refused under
  `off`. The executor (`actions.execute`, action `connector_call`) performs the MCP or API call and returns text.
- Every executed connector call is written to the Phase 0 `audit` table (`company_id`, `action`, `proposed_by`,
  `approved_by`, `payload`, `result`, `approval_id`) like any other outbound action, and the Phase 3 taint rules
  apply to it.

## Credential vault

Planned (Phase 1): `holdco/vault.py` with `put(name, value)`, `get(name) -> str | None`, `delete(name)`,
`names()`, `backend()` and `redact(text)`.

| OS | Backend | How |
|---|---|---|
| Windows | `dpapi` | `CryptProtectData` / `CryptUnprotectData` through `ctypes` with `CRYPTPROTECT_UI_FORBIDDEN`; the encrypted blobs are stored base64 in `data/vault.json`; only the same Windows user can decrypt them |
| Linux | `secret-service` | The `secret-tool` binary (`store`, `lookup`, `clear`) with attributes `app=holdco name=<name>`, used when the binary exists and a test round trip works |
| Any | `file` | The fallback below, in `data/vault.json` |

The file fallback in plain words: each secret gets a fresh random 16-byte nonce. A keystream is made by running
HMAC-SHA256 over the nonce plus an 8-byte block counter, once per block, with an encryption key; the plaintext is
XORed with that keystream. A separate MAC key produces a tag over the nonce plus the ciphertext, so tampering is
detected before decryption. Both keys are derived with HMAC from one master key. The master key is
PBKDF2-HMAC-SHA256 of the `HOLDCO_VAULT_PASSPHRASE` environment variable with a stored salt and 200,000 rounds
when that variable is set; otherwise it is 32 random bytes in `data/vault.key` with mode 0600. The backend name is
recorded with every secret, so a backup restored on another machine (or without the key file) fails loudly
instead of returning garbage.

Markers and migration: on start, Phase 1 moves plain-text secrets into the vault and leaves the marker
`vault:<name>` in their place: global `llm_api_key`, `brave_api_key`, `telegram_bot_token`, and per company
`smtp_password`, `imap_password`, `netlify_token`. `db.get_settings` and `db.company_settings` resolve markers
transparently, so `emailer.py` and `actions.py` keep working unchanged; the API masks them. New connector secrets
are written straight to the vault as `connector.<id>.<field>`; OAuth tokens as `oauth.<connector id>`.

Redaction: `vault.redact(text)` replaces every known secret value of 8 or more characters with `[redacted]`. It
runs inside `bus.emit` on the message and data, and on transcripts before `runs.transcript` is stored, so a
secret that leaks into a tool result never reaches the Feed, the Thinking tab or a session log. Agents refer to
a connector by name and never see a key. The Phase 0 backup includes the vault file.

## Essential connectors

Each entry lists: scope, fields, health check, metrics fed, tools with tags, guardrails, test mode, and what to
verify against the vendor's documentation (the plan's sources were read on Oct 14, 2026; prices and terms move).

### Built in Phase 0

#### telegram (and ntfy)

Planned (Phase 0, `holdco/notify.py`). Not a row in `connectors`: its settings live in the settings table and its
health is a Doctor row. Scope: global. Fields: `telegram_bot_token` (secret; in the vault from Phase 1),
`telegram_chat_id`, `telegram_api_base` (default `https://api.telegram.org`, pointed at `tests/fake_telegram.py`
in tests), `ntfy_topic`, `ntfy_server` (default `https://ntfy.sh`), `notify_level` (`important` | `all`). Health:
the bot's "get me" call for Telegram; "topic set" for ntfy. Metrics: none. Tools: none for agents; it is the
owner's channel. `notify.send(text, buttons, kind)` posts HTML with an inline keyboard to Telegram and plain text
to ntfy, and does not resend identical text within an hour. A long-polling thread (`getUpdates`, 25 s timeout,
restarted with backoff) handles button callbacks `a:<approval_id>:approve|reject` and
`q:<approval_id>:<option index>` through `actions.decide`, then edits the message to show the outcome.
Guardrails: updates from any chat id other than the configured one are ignored; the commands are `/status`,
`/stop` (kill switch on) and `/resume`. Test: `POST /api/notify/test`. Verify against the Telegram Bot API docs:
long-polling timeout limits and inline keyboard payloads.

### Built in Phase 1

#### stripe

Kind `api`, scope `company`. Fields: `api_key` (secret; a test key or a restricted key with the Agent tag), and
optional `use_mcp` which also attaches the hosted MCP server at `https://mcp.stripe.com` with Bearer auth. The
mode (`test` | `live`) is derived from the key prefix and shown on the setup card. Health:
`GET /v1/balance`. Metrics (polled hourly): `stripe.revenue_usd_30d`, `stripe.payments_30d`, `stripe.customers`,
`stripe.mrr_usd` (active subscriptions normalised to monthly).

| Tool | Tag |
|---|---|
| `stripe__create_payment_link(name, amount_usd, currency="usd", description="", recurring_interval="")`: creates product, price and payment link | `publish` |
| `stripe__list_payment_links()` | `read` |
| `stripe__get_payment_link(id)` | `read` |
| `stripe__recent_payments(days)` | `read` |
| `stripe__create_refund(payment_intent, amount_usd)` | `irreversible` |

MCP classification map when `use_mcp` is on (the hosted server's tool names, verified Oct 14, 2026):
`stripe_api_search`, `stripe_api_details`, `stripe_api_read`, `get_stripe_account_info`, `stripe_analytics`,
`search_stripe_documentation`, `stripe_implementation_planner` -> `read`; `stripe_api_write` -> `irreversible`
(locked), lowered to `publish` (asks) only when it creates or updates a product, a price or a payment link, and to
`spend` for a payout or a transfer; `send_money`, `transfer_money`, `convert_currency` (Treasury) -> `spend`
(locked); `setup_recipient` -> `write`; `atlas_company_formation`, `send_stripe_feedback` and any tool the map
does not name -> `irreversible`. Stripe itself requires human confirmation for some writes such as
refunds and outbound payments (a URL to approve, 24 h expiry), on top of Holdco's gate. Holdco's typed REST tools
above stay the primary path; the MCP server covers what they do not. Guardrails: refunds and payouts always
ask; a live key is flagged on the card. Test mode: a test key; the Phase 1 gate runs entirely against
`tests/fake_stripe.py`. In Phase 1 the agent's handoff is checked with `stripe__get_payment_link`; the Phase 3
verification gate retrieves any payment-link deliverable from the API. Migrate by November 30, 2026: from
December the MCP server rejects full-access secret keys and restricted keys without the Agent tag (a 401 with an
OAuth discovery challenge, which the client raises as `AuthRequired`), so create an Agent-tagged key from the
start. Still to verify against Stripe's docs: that Managed Payments makes Stripe the merchant of record for
digital products (a plan note, not a verified fact).

#### resend

Kind `api`, scope `company`. Fields: `api_key` (secret), `from_address`, `from_name`. Health: `GET /domains`.
Behaviour: when configured, the `send_email` executor sends through Resend (`POST /emails`) instead of SMTP,
chosen by the company setting `email_provider` (`auto` | `smtp` | `resend`). Inbound mail stays on IMAP, because
Resend's inbound is webhook-based and Holdco opens no ports. Metrics: `email.sent_7d` (from the `emails` table)
and `email.replies_7d` (inbound rows that reference an outbound message id). Tools: none new; `send_email` keeps
its tag (`contact`), its daily cap and the Phase 3 compliance gate, which is why the API is used rather than the
official MCP server the plan mentions. Test mode: none at Resend; Holdco's draft-only behaviour is the `approve`
policy. Verify against Resend's docs: domain verification, the send endpoint's required fields, and rate limits.

#### playwright

Kind `mcp-stdio`, scope `global`. Command: `npx -y @playwright/mcp@0.0.83 --headless --isolated --browser chromium`
(pinned; 0.0.83 was the latest release on Oct 15, 2026); config: `enabled` and a command override. Where every page
ends up is checked after each call: this PC, the home network or a local file sends the browser to about:blank
and the agent gets an error. One call at a time steers the shared browser, and a click or a typed field is tied to
the page it was asked for on. Health: start the server and list its tools. Metrics: none. Tools are
reachable through `find_tool` with this classification (the tool list verified Oct 14, 2026):
`browser_navigate`, `browser_navigate_back`, `browser_snapshot`, `browser_take_screenshot`,
`browser_console_messages`, `browser_network_requests`, `browser_resize`, `browser_wait_for`, `browser_find`,
`browser_tabs`, `browser_close` -> `read`; `browser_click`, `browser_type`, `browser_fill_form`,
`browser_press_key`, `browser_select_option`, `browser_hover`, `browser_drag`, `browser_handle_dialog` ->
`contact` (a form submit on someone else's site asks the owner); `browser_file_upload` -> `publish`;
`browser_evaluate` and `browser_run_code_unsafe` are blocked: `find_tool` never returns them and
`connectors.call` refuses them. The server's `--allowed-origins` and `--blocked-origins` flags narrow where the
browser may go but are not a security boundary; Holdco's own rules are. Guardrails: `--isolated` means the browser
never sees the owner's logins; `browser_navigate` to the local preview is allowed only through `browser_check`,
since agents are otherwise blocked from 127.0.0.1. The `browser_check(path_or_url, mobile=True)` tool
(`holdco/browser.py`, tag `read`) loads a `site/` path at `http://127.0.0.1:<port>/preview/<slug>/<path>`, waits
for load, collects console messages, a snapshot and a screenshot (saved under `.qa/`), resizes to 390x844 for a
second screenshot, checks every same-site link, checks that forms have an action or `data-netlify`, and reports
status, title, h1 and console errors. Without Playwright it falls back to a static check (fetch plus
`checks.audit_page` plus links) and says so in its first line. Test mode: not applicable. The flag names and the
tool list were read from the Playwright MCP README on Oct 14, 2026; they change between releases, so re-check
them when upgrading the package.

#### buffer

Kind `api`, scope `company`. GraphQL at `https://api.buffer.com/` with a personal access token. Fields: `api_key`
(secret), `default_channels`. Health: a small account query. Metrics: none in Phase 1. Tools:
`buffer__list_channels()` [`read`], `buffer__create_post(text, channel_ids, link, schedule)` [`publish`].
Behaviour: when configured, the `social_post` executor posts through Buffer to the card's default channels of the
post's platform, channel by channel (a retry skips the channels that already went out); without default channels,
or none for that platform, the webhook or the manual link as today, with a note. Guardrails: `social_post` stays under its policy, and Phase 2 adds the `posts_per_week` budget.
Test mode: none; keep the policy on `approve`. Verify against Buffer's docs: the plan calls the GraphQL API beta
and says one key posts to 11 networks; confirm the endpoint, the token type and channel ids.

#### brave

Kind `builtin`, scope `global`. The `brave_api_key` moves to the vault. Health: one search. Metrics: none. Tools:
none new; it is one provider in the search chain, which stays in `web.py`. Test mode: not applicable. Verify
against Brave's docs: plan limits and the subscription header.

### Built in Phase 4

Scope and exact tool names for these are fixed in [phases/phase-4-scale.md](phases/phase-4-scale.md); the tools are
described in words here.

#### porkbun

Kind `api`. Tools: check availability and pricing (`read`); DNS record tools (tags fixed in Phase 4); register
a domain (`spend` and `irreversible`, always asks, routed through `spend_money` which is locked). Guardrails: the
sandbox key and `dryRun` are supported and on by default; the setup card tells the owner to create a key without
purchase rights until they want purchases. Verify against Porkbun's docs: the plan says the service is built for
agents with scoped keys, dry runs, idempotency and a $100/month default agent spend cap, and offers an official
MCP server at mcp.porkbun.com as an alternative to the API.

#### cloudflare

Kind `api`. Deploys `site/` as Workers static assets through the API; deploys are `publish`.
It is an alternative target for the existing `deploy_site` executor, chosen per company; Netlify stays. Verify
against Cloudflare's docs: the plan cites the Pages-to-Workers migration guide; confirm the upload flow and the
token permissions before connecting.

#### github

Kind `mcp-http` (the official hosted server) or `mcp-stdio` through Docker. Tags: the read-only
toolset for every role; engineering also gets the issues and pull request write tools, tagged `write`
(`connector_write`). Verify against the GitHub MCP server README: the read-only flag, the toolset names and the
token scopes.

#### plausible and umami

Kind `api`, polled. Metrics: `analytics.visitors`, `analytics.visits`, `analytics.conversions`.
No tools beyond reads. Verify against Plausible's docs: the plan notes the Stats API is a Business-plan feature
on Plausible's cloud; self-hosted Umami needs its own API token.

#### forms

Kind `api`, polled. Reads Netlify Forms submissions, or an optional Cloudflare Worker endpoint
polled the same way. Metrics: `forms.signups`. Side effect: each submission becomes a CRM contact with `consent`
and `source` set ([operating-model.md](operating-model.md)). Verify against the Netlify Forms API docs: the plan
only states that submissions can be polled.

#### n8n

Kind `api` (its webhook) or MCP (its server). The catch-all for services without a connector. Calls through its
webhook are outbound posts and therefore `publish`; tools from its MCP server are unclassified unless Phase 4 adds
a classification map, so they ask by default (`connector_publish`). Verify against n8n's docs for its MCP server
before relying on it.

### Later tier

`comfyui`, `ads` and `accounting` ship in Phase 4 as documented cards marked "not yet available" with the reason:

| Key | Why it waits |
|---|---|
| `comfyui` | Local image generation shares the one GPU with the language model, so it must be scheduled to run only while the model is idle |
| `ads` | Ad platforms spend money continuously; they need hard daily caps on top of the locked `spend_money` policy, and revenue to justify them |
| `accounting` | QuickBooks or Xero only pay off once there is revenue to book |

The plan also lists CRM sync (HubSpot or Attio) for later; it has no card yet.

## The search chain

Planned (Phase 0, `holdco/web.py`; see [reliability.md](reliability.md) for the Doctor and preflight side).

Providers: `brave` (needs `brave_api_key`), `searxng` (needs `searxng_url`), `ddgs` (the optional package),
`ddg_html` (the html and lite endpoints). The setting `search_provider` keeps its three values; `duckduckgo`
means `ddgs` then `ddg_html`.

Order: the configured provider first, then the rest in the fixed order `brave`, `searxng`, `ddgs`, `ddg_html`.
Providers that lack their key or URL are skipped and reported as "no key" or "no url".

Blocked detection: a provider that raises, returns no results, or returns a bot-challenge page (DuckDuckGo's
"anomaly" or challenge page, or HTTP 202, 403 or 429) is marked blocked.

`web.PROVIDER_STATUS[name]` holds `{ok, last_ok, last_error, error, cooldown_until}`. A blocked or failed provider
gets `cooldown_until` 10 minutes ahead and is skipped while cooling. The Doctor's `search` row reads this table;
`deep=true` runs a live query through the chain.

`search()` returns the first non-empty result set and records which provider answered. If every provider fails it
raises `WebError("All search providers failed: duckduckgo: blocked (HTTP 202 challenge); brave: no key; ...")`.

What the agent sees: the `web_search` tool turns that into a clear instruction, continue with sources it already
knows, fetch known URLs directly, and record the gap with `note_progress`, instead of today's "No results". One
`error` event per 30 minutes tells the owner once rather than once per call. The founding preflight treats search
as a soft check: a company founds anyway and the CEO is told to skip research and note the gap.
