# Phase 1: Connect

Phase 1 gives the companies eyes and hands outside the PC. It adds a credential vault so keys leave the plain-text
settings table, a standard-library MCP client that speaks both protocol generations over stdio and Streamable
HTTP, an OAuth flow for hosted servers, a risk tag on every tool that maps onto the approval policies, a
connector framework with a registry, setup cards, health checks and metric pipes, a `find_tool` tool so role
belts stay small, five connectors (Stripe, Resend, Playwright MCP, Buffer, Brave), and a `browser_check` tool
with a static fallback. The reasons are in [../plan.md](../plan.md) ("Connectors and MCP") and
[../connectors.md](../connectors.md); the safety rules in [../safety.md](../safety.md). This document is the "how".

Status: built. Branch `claude/phase-1-connect`, PR based on Phase 0.5 (see
[phase-0.5-ground-truth-and-context.md](phase-0.5-ground-truth-and-context.md), itself based on
[phase-0-stabilize.md](phase-0-stabilize.md)). Covered by about 650 tests on Python 3.10, 3.12 and 3.13, including
the Stripe gate end to end with the scripted model, and re-run on the owner's RTX 5090: gpt-oss:20b, given a plain
task with no tool named, used `find_tool` to load Stripe's tools, created the $19/month payment link and finished;
a test charge then appeared as revenue with source `stripe`, typed by nobody. The model skipped `browser_check`
although the acceptance asked for it, which the Phase 3 verification gate addresses.

Changes Phase 0.5 makes to this design: `find_tool` reaches every role through `roles.CONTEXT_EXTRA` instead of every
preset's list; the Phase 0.5 tools carry risk tags (search, grep, company_status, check_sources read; edit_file,
record_fact write); built-in tags come from `tools.BUILTIN_RISK`; E2E tests are numbered from `test_26`; the facts
block (`truth.facts_section`) lists connected services as accounts the company has, and revenue from a connector
counts as a real signal (independent review: rows a connector polled in test mode are shown labelled "(test mode)"
and never count); secrets in `records/` files are redacted like events and transcripts.

Related: [../architecture.md](../architecture.md), [../operating-model.md](../operating-model.md),
[../reliability.md](../reliability.md), [../owner-experience.md](../owner-experience.md),
[../testing.md](../testing.md), [phase-2-organize.md](phase-2-organize.md), [phase-3-verify.md](phase-3-verify.md).

## Scope

From the plan's Phase 1 and the brief's list:

- Vault (`vault.py`): DPAPI on Windows, Secret Service on Linux, passphrase-protected file fallback; migration
  of existing plain-text secrets; redaction of secrets from events and transcripts.
- MCP client (`mcp.py`): JSON-RPC over stdio and Streamable HTTP, `server/discover` (2026-07-28) with fallback
  to `initialize` (2025-11-25 and older).
- OAuth (`oauth.py`): authorization code with PKCE, discovery, dynamic registration, tokens in the vault.
- Risk tags on every tool and four new approval policies.
- Connector framework (`connectors.py`): definitions, instances, setup cards, health checks, metric pipes, API.
- `find_tool` and routing of loaded connector tools through the risk gate.
- Connectors: `stripe`, `resend`, `playwright`, `buffer`, `brave`.
- `browser_check` (`browser.py`) with a static fallback.
- Fakes: `fake_mcp_server`, `fake_stripe`, `fake_resend`, `fake_buffer`, `fake_oauth`; `tests/test_phase1.py`.

Gate: in Stripe test mode, an agent creates a payment link, a browser check confirms it opens, and a test
purchase shows up as revenue without anyone typing it.

## What the first real run showed

The plan's table for today: "No MCP, no payments, no analytics and no browser. Keys sit in plain text in the
database." and "Metrics: `record_metric`, typed in by agents. Self-reported." In the code:

| Area | Today | Gap this phase closes |
|---|---|---|
| Secrets | `smtp_password`, `imap_password`, `netlify_token` live in `companies.settings` JSON; `llm_api_key`, `brave_api_key` (and from Phase 0 `telegram_bot_token`) in the `settings` table. `server._public_company` masks the three company keys with `••••••••`; `GET /api/settings` returns the global keys in clear. Transcripts and events are stored as written. | Vault, markers, masking of every secret, redaction before storage. |
| Metrics | `tools.record_metric` inserts `(company_id, agent_id, name, value, note)`; nothing else writes the table; the Overview tiles show whatever the agent typed. | `metrics.source` column; `connectors.poll_metrics()` writes rows with `source = <connector key>`. |
| Outside services | `tools.http_request` (raw HTTP behind `http_post`), `emailer` (SMTP/IMAP), `actions._deploy_netlify`, `actions._social` (webhook or an X intent link), `web.search` (Phase 0 chain). Every policy is per action type, all or nothing. | MCP client, connector registry, risk tags, per-tag policies. |
| Browser | None. `checks.audit_page` parses HTML statically; nothing renders a page or submits a form. | Playwright MCP through the connector framework; `browser_check` with a static fallback. |
| Tool belts | 9 to 14 tools per role from `presets.PRESETS[...]["tools"]`; `tools.specs()` sends only those. | `find_tool` loads up to 3 connector tools into a session on demand (`ctx.loaded_tools`). |

## Design

Conventions as in Phase 0. Outbound HTTP from connectors uses `urllib.request` directly with the instance's
`api_base` (not `web.check_public_url`, which blocks loopback, because tests point `api_base` at fakes on
127.0.0.1). Secrets reach connector code through `vault.get` and are never put in prompts, events, transcripts
or tool results; `vault.redact` is the backstop.

### 1. Vault (`vault.py`)

Behaviour. `vault.put(name, value)`, `vault.get(name) -> str | None`, `vault.delete(name)`, `vault.names() -> list[str]`,
`vault.backend() -> "dpapi" | "secret-service" | "file"`, `vault.redact(text) -> str`. Names are dotted:
`settings.<key>` for global secrets, `company.<id>.<key>` for company secrets, `connector.<id>.<field>` for
connector fields, `oauth.<connector id>` for token sets (JSON string).

Backend selection at first use (cached): Windows → `dpapi`; Linux → `secret-service` when `shutil.which("secret-tool")`
exists and a store/lookup/clear round trip of a probe secret works; otherwise `file`. The environment variable
`HOLDCO_VAULT_BACKEND` (`auto` default, or one of the three names) forces a backend; tests set `file` so they
never touch a developer's keyring.

| Backend | Storage | Mechanism |
|---|---|---|
| `dpapi` | `data/vault.json`: `{"<name>": {"backend": "dpapi", "blob": base64}}` | `ctypes` calls to `CryptProtectData` / `CryptUnprotectData` with `CRYPTPROTECT_UI_FORBIDDEN`; user scope |
| `secret-service` | the desktop keyring; `data/vault.json` keeps `{"backend": "secret-service"}` per name so `names()` works | `secret-tool store --label "Holdco <name>" app holdco name <name>` (value on stdin), `secret-tool lookup app holdco name <name>`, `secret-tool clear app holdco name <name>` |
| `file` | `data/vault.json`: `{"version": 1, "salt": base64, "secrets": {"<name>": {"backend": "file", "nonce": base64, "ct": base64, "tag": base64}}}` | master key = `PBKDF2-HMAC-SHA256(HOLDCO_VAULT_PASSPHRASE, salt, 200000)` when the env var is set, else 32 random bytes in `data/vault.key` (mode 0600, created with `os.open(..., 0o600)`); `enc_key = HMAC-SHA256(master, b"enc")`, `mac_key = HMAC-SHA256(master, b"mac")`; per secret a random 16-byte nonce; keystream block i = `HMAC-SHA256(enc_key, nonce + i.to_bytes(8, "big"))`, XORed with the plaintext; `tag = HMAC-SHA256(mac_key, nonce + ciphertext)`; `get` verifies the tag with `hmac.compare_digest` before decrypting |

The backend name is recorded per secret. `get` reads a secret with the backend it was stored with when that backend
works here; one whose backend does not work here raises
`VaultError("Secret <name> was stored with <backend> on another machine; enter it again in Settings")`, so a
backup restored elsewhere fails loudly. The Doctor gets a row `vault` (ok: backend name; bad: unreadable file or
a secret with a foreign backend). Independent review: a brief secret-tool outage at start makes the active backend
`file`, but the vault's recorded backend is not changed while secrets stored with it remain; new secrets go to the
file (recorded per secret), keyring secrets read again once the keyring answers, and the Doctor's row warns
("keyring unavailable; new secrets are stored in the file vault") instead of failing. Only the keys in
`SECRET_SETTINGS` and `SECRET_COMPANY_SETTINGS` resolve `vault:` markers.

Migration on start. `db.init()` ends with `vault.migrate()` (lazy import): for each key in
`config.SECRET_SETTINGS = ("llm_api_key", "brave_api_key", "telegram_bot_token")` whose stored value is non-empty
and does not start with `vault:`, `put("settings.<key>", value)` and store the marker `vault:settings.<key>`; for
each company and each key in `config.SECRET_COMPANY_SETTINGS = ("smtp_password", "imap_password", "netlify_token")`
likewise with `company.<id>.<key>`. Readers resolve markers: `db.get_settings` and `db.company_settings` replace
any string value starting with `vault:` by `vault.get(name) or ""`. Writers route secrets: `server.api_settings_put`
and `server.api_company_patch` call `vault.put` and store the marker for secret keys, skip the mask `••••••••`,
and delete the secret when the value is empty. `GET /api/settings` masks `SECRET_SETTINGS` the way
`_public_company` masks company keys. The vault file is included in backups (Phase 0 lists it).

Redaction. `vault.redact(text)` replaces every known secret value of 8 characters or more (the cache of values
loaded so far plus a `names()` sweep on first call) with `[redacted]`. Applied in `bus.emit` to `message` and to
the JSON of `data`, in `agents._finalize` to the transcript JSON before `db.update("runs", ...)`, and in
`actions.audit` to `payload` and `result` before the row is written, so a secret that leaks into a connector tool
result never reaches the audit log either.

Data. `data/vault.json`, `data/vault.key`. No tables.

API and UI. No new routes. Settings and company Settings forms are unchanged except that secret fields now show
the mask when set. The Doctor shows the `vault` row.

Code touch points. New `holdco/vault.py` (`VaultError`, the three backends behind one interface, `migrate`,
`redact`). `config.SECRET_SETTINGS`, `config.SECRET_COMPANY_SETTINGS`. `db.init`, `db.get_settings`,
`db.company_settings`. `server.api_settings`, `api_settings_put`, `api_company_patch`, `_public_company`.
`bus.emit`, `agents._finalize`, `actions.audit`. `backup.run_backup` already lists the files.

### 2. MCP client (`mcp.py`)

Behaviour. `mcp.Client(name, transport, command=None, args=(), env=None, url=None, headers=None, auth=None,
connect_timeout=None)` with `transport` in `stdio | http`. Methods: `connect()`, `list_tools() -> list[dict]` (each
`{name, description, inputSchema}`), `call_tool(name, args) -> {"content": [...], "isError": bool,
"structuredContent": ...}`, `close()`, and the module function `mcp.text_of(result) -> str`. JSON-RPC 2.0
with integer ids; `connect` has a 30 s timeout (`connect_timeout` overrides it: the framework passes a stdio
definition's `health_timeout`, 120 s for Playwright's first start), `call_tool` 120 s, and the deadline holds
while an HTTP reply arrives; an HTTP reply or one SSE event over 5 MB raises; a timeout raises `mcp.MCPError`. In the
current generation every request carries the namespaced `_meta` keys (see below). `client.protocol` records the
generation in use (`"2026-07-28"` or the version the server returned on `initialize`).

Transports:

- `stdio`: `subprocess.Popen` of `command` resolved with `shutil.which` (so `npx` finds `npx.cmd` on Windows),
  with `env` merged over `os.environ` without its `HOLDCO_*` and `OLLAMA_*` variables (the vault passphrase and
  the dashboard password never reach a server unless its `env` sets one), `stdin`/`stdout` pipes, newline-delimited
  JSON frames, a reader thread
  that dispatches responses by id into per-request `queue.Queue`s and ignores notifications, and `stderr`
  captured into a ring buffer (last 4,000 chars) that is included in error messages. `close()` ends the process
  (`terminate`, then `kill` after 5 s).
- `http` (Streamable HTTP): `POST url` with `Content-Type: application/json` and
  `Accept: application/json, text/event-stream`. A JSON response is one message. An SSE response
  (`Content-Type: text/event-stream`) is read line by line: `data:` lines are accumulated per event and parsed;
  the first message whose `id` matches ours is the reply; the stream is closed afterwards (a stream that breaks
  before the reply is not resumed: the request is re-issued with a new id). In the current generation every POST
  carries the `Mcp-Method` header (the JSON-RPC method) and the `Mcp-Name` header (the name the request targets,
  such as the tool name on `tools/call`), and there is no session id. In the older generation only, when the
  server sets `Mcp-Session-Id`, every later request echoes it, and the `MCP-Protocol-Version` header carries
  `client.protocol` after `initialize`. `headers` (for example `Authorization: Bearer <key>`) and `auth` (an
  `oauth.TokenSource` that supplies and refreshes a bearer token) are applied to every request. An HTTP 401 with a
  `WWW-Authenticate` header raises `mcp.AuthRequired(resource_metadata_url)`, where the URL comes from the
  header's `resource_metadata` parameter or defaults to `<origin>/.well-known/oauth-protected-resource`.

Negotiation in `connect()` (the MCP 2026-07-28 facts below were verified on Oct 14, 2026):

1. Send `server/discover` with `params._meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
   "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": {"name": "holdco",
   "version": <holdco version>}}`. If the reply is a result, the server speaks the current stateless generation:
   `protocol = "2026-07-28"`, the server's protocol versions, capabilities and identity are kept, there is no
   handshake and no `ping`, and every later request carries those three `_meta` keys (and, over HTTP, the
   `Mcp-Method` and `Mcp-Name` headers). On every result the client reads
   `_meta["io.modelcontextprotocol/serverInfo"]` (kept as `client.server_info`) and `resultType`: missing or
   `"complete"` is a finished result; `"input_required"` means the server wants more input in a further round
   trip, which Holdco cannot provide, so `call_tool` raises `MCPError("the server needs input Holdco cannot
   provide")` and the agent sees it as an `ERROR:` text. A JSON-RPC error `-32022` on any request raises
   `MCPError("unsupported protocol version")`.
2. If `server/discover` fails with JSON-RPC error `-32601` or `-32600`, or with an HTTP 4xx or 5xx (a 401 still
   raises `AuthRequired`), send `initialize` with `protocolVersion "2025-11-25"`, `clientInfo {"name": "holdco",
   "version": <holdco version>}`, `capabilities {}`; accept the version the server returns when it is one of
   `2025-11-25`, `2025-06-18`, `2025-03-26` (else `MCPError("unsupported protocol version")`), then send the
   notification `notifications/initialized`. `protocol` records the returned version. Only in this generation
   does the HTTP transport echo `Mcp-Session-Id` and send `MCP-Protocol-Version`.
3. Anything else raises `MCPError` with the transport's detail (stderr tail for stdio, status and body prefix
   for HTTP).

Tool results. `text_of(result)` joins the `text` of text content blocks with newlines; an image block becomes
`[image <mimeType>, <n> bytes]`, a resource block `[resource <uri>, <mimeType>, <n> bytes]`; when `isError` is
true the text is prefixed with `ERROR: `; `structuredContent`, when present and no text was given, is
`json.dumps`'d. The method names, `_meta` keys, headers and error codes above are the verified 2026-07-28 and
2025-11-25 facts from the brief. A current-generation `tools/list` result may also carry `ttlMs` and `cacheScope`;
the client passes them through and does not act on them in this phase.

Data. None.

Code touch points. New `holdco/mcp.py` (`Client`, `MCPError`, `AuthRequired`, `text_of`, `_StdioTransport`,
`_HttpTransport` (adds the `Mcp-Method` and `Mcp-Name` headers in the current generation, `Mcp-Session-Id` and
`MCP-Protocol-Version` in the older one), `_meta_for(method, params)` (the namespaced `_meta` keys),
`_check_result(reply)` (`resultType` and `-32022`), `_parse_sse`). No changes elsewhere; `connectors.py` is the
only caller.

### 3. OAuth (`oauth.py`)

Behaviour. Authorization code with PKCE (`S256`), a random `state`, and the redirect URI
`http://127.0.0.1:<dashboard port>/oauth/callback` served by the dashboard. The dashboard port is `config.PORT`,
which `run.py` must set from `--port` (today it only passes the argument to `make_server`).

Discovery, in order, all over HTTPS except the fakes: protected resource metadata (RFC 9728) from the
`resource_metadata` URL in `WWW-Authenticate` or `<resource origin>/.well-known/oauth-protected-resource`, which
names the authorization server; then authorization server metadata (RFC 8414) at
`/.well-known/oauth-authorization-server`, falling back to `/.well-known/openid-configuration`; then dynamic
client registration (RFC 7591, deprecated by the MCP specification in favour of Client ID Metadata Documents but
still allowed) at the metadata's registration endpoint when present, else the `client_id` from the connector's
config (`oauth_client_id` field). The flow:

| Step | Function | Detail |
|---|---|---|
| start | `oauth.start(connector_id) -> str` | discovers (cached per issuer in `oauth._meta`), registers if needed, creates `verifier`/`challenge`/`state`, stores `oauth._pending[state] = {connector_id, verifier, token_endpoint, issuer, resource, client_id, created_at}` (expires after 10 min), returns the authorization URL with `response_type=code`, `client_id`, `redirect_uri`, `code_challenge`, `code_challenge_method=S256`, `state`, and `resource` (the MCP server URL) |
| finish | `oauth.finish(query) -> dict` | looks up `state`; refuses the callback with `OAuthError` when the query carries `iss` and it differs from `_pending[state].issuer` (the issuer recorded at start), storing nothing; otherwise posts `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `code_verifier` to the token endpoint, stores `{"access_token", "refresh_token", "expires_at", "issuer", "resource", "client_id", "token_endpoint"}` in the vault as `oauth.<connector id>`, marks the connector instance for a health re-check, returns the connector row |
| use | `oauth.TokenSource(connector_id)` | `.token()` returns the access token; when fewer than 60 s remain it posts `grant_type=refresh_token` and stores the new set; tokens are sent only to the stored `resource` origin (the `mcp.Client` asks the source for the URL it is about to call and the source refuses any other origin with `OAuthError`) |

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/connectors/<id>/oauth/start` | `{}` | `{"url": str}` (the dashboard opens it in a new tab) |
| GET | `/oauth/callback?code=&state=` | none | an HTML page "Connected <label>. You can close this tab." (or the error text), status 200; not an `/api/` route: `server.Handler._handle` checks `path == "/oauth/callback"` before `_static` |

UI. The connector setup card (item 5) shows a `Sign in` button for `mcp-http` connectors that need OAuth; after
the callback the card shows "Signed in" and the health status. The callback page also tells the owner to close
the tab.

Code touch points. New `holdco/oauth.py` (`start`, `finish`, `TokenSource`, `OAuthError`, `_discover`,
`_register`, `_pkce`). `server.Handler._handle`: the callback branch. `run.py`: `config.PORT = args.port`,
`config.HOST = args.host`.

### 4. Risk tags and policies

Behaviour. `config.RISK_TAGS = ("read", "write", "publish", "contact", "spend", "irreversible")`. Every tool has
one. `tools.tool(name, description, props, required=(), risk="write")` stores it on `Tool.risk`; `tools.risk_of(name)`
returns it. Built-in tags:

| Tag | Built-in tools | Policy that applies |
|---|---|---|
| `read` | `web_search`, `fetch_url`, `read_file`, `list_files`, `recall`, `list_tasks`, `check_inbox`, `audit_page`, `check_contrast`, `review_text`, `browser_check`, `find_tool` | none (runs freely) |
| `write` | `write_file`, `delete_file`, `create_task`, `update_task`, `remember`, `record_metric`, `update_charter`, `hire_agent`, `note_progress`, `mark_email_handled`, `report_to_owner`, `ask_owner`, `finish`, `run_code` | none for internal writes; `run_code` keeps its own `run_code` policy |
| `contact` | `send_email` | `send_email` (unchanged) |
| `publish` | `social_post`, `deploy_site`, `http_request` (non-GET) | `social_post`, `deploy_site`, `http_post` (unchanged) |
| `spend` | `request_purchase` | `spend_money` (locked) |

For built-in tools the tag is descriptive: the existing policy keys stay. For connector tools the tag decides the
policy:

| Connector tool tag | Policy key | Label in `config.POLICY_ACTIONS` | Default |
|---|---|---|---|
| `read` | none | | runs freely |
| `write` | `connector_write` | "Change data in a connected service" | `approve` |
| `publish` | `connector_publish` | "Publish through a connected service" | `approve` |
| `contact` | `connector_contact` | "Message people through a connected service" | `approve` |
| `spend` | `spend_money` | existing | locked `approve` |
| `irreversible` | `irreversible` | "Irreversible actions: domains, deletions, refunds" | locked `approve` |
| unclassified (an MCP tool no map matched) | `connector_publish` | | asks by default |

`config.LOCKED_POLICIES = {"spend_money", "irreversible"}`. The Settings page and company Settings tab render
policies from `policy_actions`, so the four new rows appear without JS changes; locked ones show "Always asks
you".

UI. `GET /api/approvals` adds `tag` to every row (`actions.TAG_OF_ACTION` for built-ins, `payload.tag` for
`connector_call`). `requestCard` shows it as a `.badge` (`accent` for read/write, `warn` for publish/contact,
`bad` for spend/irreversible) next to the request number. The connector setup card lists each tool with its tag.

Code touch points. `config.py` (`RISK_TAGS`, `POLICY_ACTIONS`, `DEFAULT_POLICIES`, `LOCKED_POLICIES`).
`tools.tool`, `tools.Tool.risk`, `tools.risk_of`, the `risk=` argument on every `@tool` in `tools.py` and
`watchdog.py`. `actions.TAG_OF_ACTION`, `server.api_approvals`. `app.js` `requestCard`.
`tests/test_unit.py::TestPresets.test_integrity` gains "every registered tool has a tag in `RISK_TAGS`".

### 5. Connector framework (`connectors.py`)

Behaviour. `connectors.DEFINITIONS: dict[str, dict]` keyed by connector key. A definition:

| Field | Type | Meaning |
|---|---|---|
| `label`, `category` | str | shown on cards ("Payments", "Email", "Browser", "Social", "Search") |
| `kind` | `api` / `mcp-stdio` / `mcp-http` / `builtin` | how calls are made |
| `scope` | `global` / `company` | one instance for all companies, or one per company |
| `setup` | dict | `where_to_get_key` (text), `permissions` (list of str to tick), `fields` (list of `{key, label, secret: bool, hint, placeholder}`), `test_mode` (text), `docs_url`, `agents_can` (list of str) |
| `health(inst)` | callable → `(status, detail)` | `status` in ok/warn/bad; must finish within 15 s |
| `metrics(inst, company)` | callable → `[(name, value, note)]` | names without the connector prefix |
| `tools(inst)` | callable → list of tool specs `{name, description, parameters, tag, fn}` | `api` connectors only; `fn(inst, company, **args) -> str` |
| `mcp` | dict | `mcp-*` kinds: `command`, `args`, `env` or `url`, `headers`; `classify` (ordered `{glob: tag}`); `allow` (`{role or "*": [globs]}`, default `{"*": ["read-tagged tools"]}`); `blocked` (list of tool names never exposed: left out of the catalog and refused by `connectors.call`) |
| `test_mode(inst)` | callable → bool | shown on the card and in the agents' prompt line |

Instances live in a new table:

| Column | Type | Content |
|---|---|---|
| `id` | INTEGER PK | |
| `company_id` | INTEGER NULL | NULL for global scope |
| `key` | TEXT | definition key |
| `enabled` | INTEGER DEFAULT 1 | |
| `config` | TEXT JSON | non-secret fields only, plus `api_base` when overridden (fakes; `api_base` and hidden fields such as `mcp_url` are accepted by `POST /api/connectors` only while `HOLDCO_ALLOW_LOCAL` is set, else 400 "<field> can only be changed in a test setup") |
| `status`, `detail` | TEXT | last health result |
| `last_check` | TEXT | iso |
| `created_at` | TEXT | iso |

Secret fields go to the vault as `connector.<id>.<field>`; `connectors.secrets(inst) -> dict` resolves them.
`connectors.instance(key, company) -> row | None` returns the company's instance when one exists, else the
global one, else None. One instance per `(key, company_id)`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/connectors` (`?company_id=` optional) | none | `{"definitions": {key: setup card + kind + scope + tools with tags}, "instances": [rows with config and masked secret fields], "statuses": {instance id: {status, detail, last_check}}}` |
| POST | `/api/connectors` | `{"id"?: int, "key": str, "company_id": int|null, "enabled": bool, "config": {field: value}}` (secret fields with the mask are unchanged) | `{"instance": row, "health": {"status", "detail"}}`; runs the health check synchronously |
| POST | `/api/connectors/<id>/test` | `{}` | `{"health": {"status", "detail"}, "metrics": [{name, value, note}]}`; runs health and, when ok, one metrics poll which is also written to the table |
| DELETE | `/api/connectors/<id>` | `{}` | `{"ok": true}`; deletes the row and its vault secrets, closes any MCP client |
| POST | `/api/connectors/<id>/oauth/start` | `{}` | `{"url": str}` (item 3) |

Health checks run on save and hourly from `scheduler._housekeeping` (`connectors.check_if_stale()`, interval
from `HOLDCO_CONNECTOR_CHECK_SECONDS`, default 3600). A failing instance: Doctor row `connector:<id>` (label
"<label> (<company name or global>)", status from the check, fix link to the card); `notify.send` once per day
per instance (`connectors._alerted[id] = date`); and the agents' system prompt lists it under "Connectors that
are down right now" (`connectors.down_for(company) -> [str]`, each "<label>: <detail>. Do not plan work that
depends on it."). The watchdog's `connector_down` pattern applies to `connector:*` rows (Phase 0, item 3).

Metrics pipes. `connectors.poll_metrics()` runs hourly (`HOLDCO_METRICS_POLL_SECONDS`, default 3600) from
`_housekeeping` in a thread: for every enabled instance with a `metrics` function and every company it applies
to, insert rows `metrics(company_id, agent_id NULL, name "<key>.<metric>", value, note, source <key>)`. New
column `metrics.source TEXT DEFAULT 'manual'` (MIGRATIONS; existing rows read `manual`). `tools.record_metric`
keeps writing `manual`. `agents._metrics_text` shows `(source: stripe)` or `(manual)`; `server.api_company`
returns `source`; the Overview tile shows it in the `title`. Nothing opens a port: every connector is polled.

System prompt. `agents.system_prompt` adds one short section after Team: "# Connected services\n- stripe (test
mode), resend, playwright\nUse find_tool to load a tool from them." and, when any instance is down, "Connectors
that are down right now:" with the lines above.

UI. A `Connectors` section at the top of the company Settings tab (company scope) and a `Connectors` section on
the global Settings page (global scope). Each definition is a card: label, category, kind, what agents can do
(bullets), tools with tag badges, "Where to get the key" and "Permissions to tick" text, the test-mode note, the
fields (`.form-grid`, password inputs for secrets), `Save and test`, `Test`, `Remove`, `Sign in` (OAuth), the
last health result as a `.badge`, and the last polled metrics. `data-form="connector-<key>"` submits
`POST /api/connectors`.

Code touch points. New `holdco/connectors.py` (`DEFINITIONS` assembled from `conn_*.py` modules, `instance`,
`secrets`, `check(inst)`, `check_if_stale`, `poll_metrics`, `down_for`, `catalog`, `call`, `execute`, routes).
`db.SCHEMA` (`connectors` table), `db.MIGRATIONS["metrics"]`. `scheduler._housekeeping` adds
`connectors.check_if_stale` and `connectors.poll_if_due`. `doctor.run` adds `connector:*` rows and the `vault`
row. `agents.system_prompt`, `agents._metrics_text`, `server.api_company`. `app.js`: `connectorCards()` used by
`Settings()` and `TabSettings()`.

### 6. `find_tool` and tool routing

Behaviour. `find_tool(query)` (tag `read`, in every role's belt, every run kind) searches the catalog of enabled
connectors visible to the company (its own instances plus global ones): for `api` connectors the definition's
`tools(inst)`; for MCP connectors `Client.list_tools()` cached per instance for 10 minutes
(`connectors._tool_cache`). Each catalog entry is `{name: "<key>__<tool>", description, parameters (the MCP
inputSchema, or the api spec), tag, instance_id, allowed}`; `allowed` comes from the definition's `allow` map
for the agent's role (`api` connectors allow every role; MCP connectors allow read-tagged tools for every role
and the rest only for roles named in `allow`). Scoring: the number of query words (lowercased, length >= 3)
found in name or description, ties broken by name. The result text lists up to 5 matches as
`<name> [<tag>]: <description first 120 chars>` and ends with "Loaded: <top 3 names>. You can call them now."
The top 3 are appended to `ctx.loaded_tools` (deduplicated); no match returns "No connected tool matches
'<query>'. Connected services: <list>." Loaded specs carry `parameters` with only `type`, `properties`,
`required` and `description` keys (MCP schemas can be large; anything else is dropped).

`agents.run` rebuilds `tool_specs = base_specs + ctx.loaded_tools` and `ctx.allowed` after any step in which
`len(ctx.loaded_tools)` changed, so the model sees the new tools on its next call. `tools.execute` checks
`ctx.loaded_by_name` before `REGISTRY` and routes a loaded tool to `connectors.call(ctx, name, args)` after
`tools.clean_args(spec, args)` (the `_clean_args` logic factored out to accept a spec dict).

`connectors.call(ctx, name, args) -> str`:

1. Resolve instance and catalog entry; refuse with `ERROR: <name> is not allowed for your role.` when not allowed.
2. `tag == "read"`: run `connectors.execute(payload)` directly, log `bus.emit(cid, "tool", "called stripe
   list_payment_links", agent_id)`, return the text.
3. Otherwise build `payload = {"connector_id", "connector": key, "tool", "args", "tag"}` and return
   `actions.gate(company, agent, task_id, "connector_call", payload, policy_action=<mapping from item 4>)`.
   `gate` gains the keyword `policy_action: str | None = None` and uses it for the policy lookup when given.

`actions.execute` gains `if action == "connector_call": return connectors.execute(p, company=company,
approval_id=...)`. `connectors.execute(payload, company=None, approval_id=None) -> str` first checks that the
instance is still the one the request was made for (its key is `payload.connector` and its company is the
request's or none; SQLite reuses a deleted row's id), else `RuntimeError("... ask again")`; then `api` → the spec's
`fn(inst, company, **args)` (plus `approval_id=` for a spec with `takes_approval_id`); `mcp-*` →
`connectors.client(inst).call_tool(tool, args)` under the instance's call lock (one call at a time; `browser_check`
holds it for its whole sequence) then `mcp.text_of(result)`; an `isError` result or an
`MCPError` raises `RuntimeError(text)` so `gate` reports `FAILED:` and the approval row records the failure.
A definition's `check_result(tool, args, text)` runs after every MCP result (Playwright: a page that ended up on
this PC or the home network is left with `reset_call` and becomes an error), and a definition with `page_of` and
`page_query` ties every call that is not read to the page the browser was on when it was asked for
(`payload.page_url`): after approval, a different page refuses it ("the page changed since this was requested;
ask again"). `tag_for_call` may only raise a tag, unless the definition sets `tag_for_call_lowers` (Stripe MCP).
`actions.ACTION_LABELS["connector_call"] = "Connected service call"`; `_summary` returns
`"<connector> <tool>(<args JSON, first 90 chars>) [<tag>]"`. Every non-read connector call is audited through
`actions.audit` exactly like other outbound actions (it passes through `gate`); read calls are events only.
MCP clients are kept open in `connectors._clients[instance id]`, re-created when the process has exited, and
closed on disable, delete or a failed health check.

UI. `requestFields` adds `case 'connector_call'`: connector label, tool name, the tag badge, a read-only
`<pre class="box">` of `args`, and the connector's `agents_can` line for context.

Agent-facing changes. `find_tool` in every preset's `tools` list, in `presets.CUSTOM_TOOLS` and in
`roles.FOUNDING_TOOLS` (the preset size test allows 15; the CEO reaches 15). `_shared_rules` gains: "Outside
services are reached through find_tool; what you load may wait for the owner's approval, like email."

Code touch points. `tools.Ctx.loaded_tools: list`, `loaded_by_name: dict`; `tools.find_tool` (defined in
`connectors.py` with `@tools.tool(..., risk="read")`, imported at the bottom of `tools.py` next to `watchdog`);
`tools.execute`, `tools.clean_args`. `agents.run` (spec rebuild). `actions.gate`, `execute`, `ACTION_LABELS`,
`_summary`. `presets.py`, `roles.py`. `app.js` `requestFields`.

### 7. The five connectors

External endpoint paths, field names and query parameters below are only those the brief names; everything else
is described in words and must be verified against the vendor docs before connecting. Each `api` definition
has a hidden config field `api_base` (default the vendor's API origin) so tests can point it at a fake.

#### `stripe` (`conn_stripe.py`; kind `api`; scope `company`)

- Fields: `api_key` (secret; a test key, or an Agent-tagged restricted key for live). Mode `test | live` is
  derived from the key prefix (Stripe's test keys carry a test marker in the prefix; verify) and shown on the
  card and in the prompt line. Optional `use_mcp` (bool): attaches the hosted MCP server at
  `https://mcp.stripe.com` with `Authorization: Bearer <Agent-tagged key>` (the server accepts OAuth or an Agent
  API key as a Bearer token; Holdco uses the key it already has). A 401 with `WWW-Authenticate` (a key without
  the Agent tag, or any non-Agent key after November 30, 2026) raises `AuthRequired`, and the card then offers
  `Sign in` through item 3. Classification map (the hosted server's tool names, verified Oct 14, 2026; ordered,
  first match wins): `stripe_api_search`, `stripe_api_details`, `stripe_api_read`, `get_stripe_account_info`,
  `stripe_analytics`, `search_stripe_documentation`, `stripe_implementation_planner` → read; `stripe_api_write`
  → irreversible (locked), lowered to publish only when it POSTs to one of products, prices or payment links
  (create or update, never a deletion) and to spend for a payout or a transfer (`connectors.call` inspects the
  arguments before the gate; independent review); `send_money`, `transfer_money`, `convert_currency` (Treasury) →
  spend (locked); `setup_recipient` → write; `atlas_company_formation`, `send_stripe_feedback` and any tool the
  map does not name → irreversible.
  Stripe itself requires human confirmation (a URL to approve, 24 h expiry) for some writes such as refunds and
  outbound payments, on top of Holdco's gate. Holdco's typed REST tools below stay the primary path; the MCP
  server covers what they do not.
- Setup card: where to create a restricted key with the Agent tag (migrate by November 30, 2026: from December
  the MCP server rejects full-access secret keys and restricted keys without the Agent tag with a 401 and an
  OAuth discovery challenge, which the client surfaces as `AuthRequired`), permissions to tick (read on balance,
  charges, customers, subscriptions; write on products, prices, payment links), test-mode note ("Start with a
  test key; flip to a live key per company when you are ready"), docs link, agents can: create payment links,
  list and inspect them, read recent payments; refunds and anything with payouts always ask.
- Health: `GET /v1/balance` with Bearer auth; 200 → ok ("test mode, balance available"), 401 → bad ("key
  rejected"), network error → bad.
- Metrics (polled): `stripe.revenue_usd_30d` (sum over succeeded charges created in the last 30 days of amount
  minus refunded, in USD; non-USD charges are counted in the note, not the value), `stripe.payments_30d` (count
  of those charges), `stripe.customers` (count by paging the customers list), `stripe.mrr_usd` (active
  subscriptions: each item's unit amount times quantity normalised to a month: year/12, week*52/12, day*365/12).
- Tools:

| Tool | Tag | Does |
|---|---|---|
| `stripe__create_payment_link(name, amount_usd, currency="usd", description="", recurring_interval="")` | publish | creates a product, then a price (one-time, or recurring with the interval `month`/`year`/`week`/`day`), then a payment link with that price and quantity 1; returns "Created payment link <url> (id <id>, <name>, $<amount>[/<interval>], <mode> mode)" |
| `stripe__list_payment_links()` | read | newest 20 links: id, url, active, created |
| `stripe__get_payment_link(id)` | read | id, url, active, line item name and amount; this is how the CEO's review and the gate test check a handoff in this phase (the Phase 3 verification gate automates it) |
| `stripe__recent_payments(days=7)` | read | succeeded charges in the window: created, amount, currency, customer email when present, refunded |
| `stripe__create_refund(payment_intent, amount_usd)` | irreversible | always asks; looks up the payment's currency (whole units for zero-decimal currencies, three-decimal ones refused), then posts a refund for the amount with an `Idempotency-Key` built from the approval id (random without one) |

Stripe calls are form-encoded POSTs and GETs with Bearer auth against `api_base` (default
`https://api.stripe.com`); nested parameters (line items, recurring interval, created-date filters) use Stripe's
bracket syntax (verify).

#### `resend` (`conn_resend.py`; kind `api`; scope `company`)

- Fields: `api_key` (secret), `from_address`, `from_name`.
- Health: `GET /domains` (brief) with Bearer auth; ok when 200 and the `from_address` domain is listed as
  verified, warn when listed but not verified, bad on 401 or network error.
- Sending: new company setting `email_provider` (`auto | smtp | resend`, default `auto`) in
  `config.DEFAULT_COMPANY_SETTINGS`. `emailer.send` checks it: `resend`, or `auto` with a healthy Resend
  instance, sends `POST https://api.resend.com/emails` (verified: `Authorization: Bearer re_...`, JSON body)
  with `{from, to, subject, text, reply_to, headers: {"Message-ID": ..., "In-Reply-To": ..., "References": ...}}`
  and an `Idempotency-Key` header set to the approval id, so a retried approval cannot send twice; otherwise SMTP
  as today. The `emails` row is written the same way with the Message-ID Holdco generates
  (`email.utils.make_msgid`) in `message_id`, and the `id` Resend returns (response `{id}`) in the row's `uid`
  column, which outbound rows do not use otherwise.
- Inbound stays on IMAP (Resend inbound is webhook-based and Holdco opens no ports).
- Metrics: `email.sent_7d` (outbound `emails` rows in 7 days), `email.replies_7d` (inbound rows in 7 days whose
  new column `emails.in_reply_to` matches an outbound `message_id`, or, as a fallback, whose subject stripped of
  `Re:`/`Fwd:` prefixes equals an outbound subject sent to that address in the last 30 days).
- No tools of its own: agents keep using `send_email`.

`emailer.poll_inbox` fills `emails.in_reply_to` from the In-Reply-To header (new MIGRATIONS column).

#### `playwright` (`conn_playwright.py`; kind `mcp-stdio`; scope `global`)

- Config: `enabled`, `command` override (default `npx -y @playwright/mcp@0.0.83 --headless --isolated --browser
  chromium`, pinned; 0.0.83 was the latest release on Oct 15, 2026).
- Health: start the server and `list_tools()`; ok when `browser_navigate` is among them; warn "npx not found:
  install Node.js" when `shutil.which("npx")` is None (so CI and a PC without Node get a warning, not a daily
  alert); bad when the process starts and fails (stderr tail in the detail). The first start downloads a
  browser; the health check allows 120 s for that case.
- Classification (the tool list verified Oct 14, 2026): `browser_navigate`, `browser_navigate_back`,
  `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`,
  `browser_resize`, `browser_wait_for`, `browser_find`, `browser_tabs`, `browser_close` → read; `browser_click`,
  `browser_type`, `browser_fill_form`, `browser_press_key`, `browser_select_option`, `browser_hover`,
  `browser_drag`, `browser_handle_dialog` → contact (a form submit on someone else's site asks the owner);
  `browser_file_upload` → publish; anything else → unclassified (asks). `blocked`: `["browser_evaluate",
  "browser_run_code_unsafe"]`, a definition key (item 5) that `find_tool` never returns and `connectors.call`
  refuses with `ERROR: browser_evaluate is not available through Holdco`, whatever a role's `allow` says.
  `allow`: `{"*": [read tools], "qa": ["*"], "engineering": ["*"], "research": ["*"], "design": ["*"],
  "marketing": ["*"]}`. The server's `--allowed-origins` / `--blocked-origins` flags are not a security boundary;
  the preview rule below and the risk gate are.
- `browser_navigate` to the local preview (`127.0.0.1`, `localhost`, and the other forms a browser reads as this
  PC: `::1`, `2130706433`, `0177.0.0.1`, `127.1`) is refused through `find_tool`-loaded calls ("use browser_check
  for your own site"); only `browser_check` (item 8) may use it. `--isolated` keeps the browser profile separate
  from the owner's logins. Independent review: after every call the framework reads the result's "Page URL:" line
  (`check_result`); a page that ended up on this PC, the home network or a local file (a redirect, a link the page
  followed, a name that now resolves privately) sends the browser to about:blank and the agent gets "ERROR: the
  page went to a local or private address (<host>); Holdco blocks that". `HOLDCO_ALLOW_LOCAL` does not open this.
  Calls that are not read (click, type, upload) record the page they were asked for on and are refused after
  approval when the browser is on another page.

#### `buffer` (`conn_buffer.py`; kind `api`; scope `company`)

- Fields: `api_key` (secret, a personal access token), `default_channels` (comma-separated channel ids).
- Health: a small GraphQL account query against `https://api.buffer.com/` (brief) with Bearer auth; the query
  text and the response shape must be taken from Buffer's API docs (verify; the API is in beta).
- Tools: `buffer__list_channels()` [read] (id, service, name), `buffer__create_post(text, channel_ids, link="",
  schedule="")` [publish] (`channel_ids` defaults to `default_channels`; `schedule` is an ISO time or empty for
  "add to queue").
- `actions._social` posts through Buffer when a healthy instance exists for the company and one of its
  `default_channels` is a channel of the platform; otherwise the webhook or the manual link as today, with a note
  saying why Buffer was not used (independent review: without default channels a post would have gone to every
  channel of that platform in the Buffer account). It posts channel by channel and keeps the channels that went
  out in the request's payload (`buffer_posted`), so a retry after a partial failure skips them.

#### `brave` (kind `builtin`; scope `global`)

- The key moves from `settings.brave_api_key` to the vault (`settings.brave_api_key`, item 1); the card shows
  the field and a `Test` that runs one search through `web._brave`. Health = that search. The chain itself stays
  in `web.py` (Phase 0). No tools of its own.

Code touch points. New `conn_stripe.py`, `conn_resend.py`, `conn_playwright.py`, `conn_buffer.py`, each
exporting `DEFINITION` and tool functions; `connectors.DEFINITIONS` collects them plus the inline `brave`
definition. `emailer.send` (provider switch, `in_reply_to`), `emailer.poll_inbox`. `actions._social`.
`config.DEFAULT_COMPANY_SETTINGS["email_provider"]`. `db.MIGRATIONS["emails"]["in_reply_to"]`.

### 8. `browser_check` (`browser.py`)

Behaviour. Tool `browser_check(path_or_url, mobile=True)` (tag `read`; in the `engineering`, `qa`, `design` and
`seo` presets). For a path inside `site/`:

1. Build the preview URL `http://127.0.0.1:<config.PORT>/preview/<slug>/<path relative to site/>`. This URL is
   allowed only here: `browser.py` fetches it with `urllib` directly and passes it to Playwright without
   `web.check_public_url`.
2. With a healthy `playwright` instance: `browser_navigate` (wait for load), `browser_console_messages`,
   `browser_snapshot`, `browser_take_screenshot` saved to `<company>/.qa/<page name>.png` (the screenshot is
   written from the returned image block when the server returns one, or by passing a filename when it
   supports that), `browser_resize` to 390x844 and a second screenshot `<page name>-mobile.png`, then from the
   snapshot: every same-site link is fetched through the preview and must answer < 400; every form must have an
   `action` or a `data-netlify` attribute; the report lists status code, title, the first h1, console errors,
   link results, form results and the screenshot paths. `.qa` is added to `workspace.SKIP` so it does not clutter
   file listings; the paths are stated in the report.
3. Without Playwright (no instance, health not ok, or any MCP error): a static check whose first line is
   "Static check only: Playwright is not set up" (or "...: Playwright failed: <reason>"), followed by the
   preview fetch status, `checks.audit_page(path, site_dir, False)` and the same link and form checks done by
   parsing the HTML. When even the preview is unreachable (unit tests without a server), the file is read from
   disk and links are checked on disk.

For an external URL (`http(s)://`): `web.check_public_url`, then `web.http_get`; the report gives status, title
(from `web.html_to_text`), whether a `<form>` exists, and the first 300 characters of text. With Playwright
available the external page is also rendered for console errors, but never clicked or submitted; a page (or a
redirect of the static fetch) that ends up on this PC or the home network is an error, not a report. Each check
holds the Playwright instance's call lock for its whole sequence and closes the page (`browser_close`) at the end;
on a site page only the company's own preview may be open. For tests the
environment variable `HOLDCO_ALLOW_LOCAL` (comma-separated `host:port` pairs) exempts those origins from
`check_public_url`; it is empty by default and documented as test-only.

Result text (success example, static fallback):

```
Static check only: Playwright is not set up
GET http://127.0.0.1:8420/preview/pawplan/index.html -> 200
Title: PawPlan: weekly meal plans your dog will actually eat
H1: Meal plans your dog will eat
Links: 3 checked, 3 ok
Forms: 1 found, 1 with a target (data-netlify)
Audit: 0 blocker(s), 1 major, 2 minor (details below)
...
```

Code touch points. New `holdco/browser.py` (`browser_check` tool, `_preview_url`, `_with_playwright`,
`_static`, `_external`). `workspace.SKIP`. `web.check_public_url` (the `HOLDCO_ALLOW_LOCAL` exemption).
`presets.py` (four belts).

## Files

New: `holdco/vault.py`, `holdco/mcp.py`, `holdco/oauth.py`, `holdco/connectors.py`, `holdco/conn_stripe.py`,
`holdco/conn_resend.py`, `holdco/conn_playwright.py`, `holdco/conn_buffer.py`, `holdco/browser.py`,
`tests/fake_mcp_server.py`, `tests/fake_stripe.py`, `tests/fake_resend.py`, `tests/fake_buffer.py`,
`tests/fake_oauth.py`, `tests/test_phase1.py`, `docs/phases/phase-1-connect.md` (this file).

Changed: `holdco/config.py` (risk tags, policies, secret key lists, `email_provider`), `holdco/db.py`
(`connectors` table, `metrics.source`, `emails.in_reply_to`, marker resolution, `vault.migrate` call),
`holdco/tools.py` (risk tags, `clean_args`, loaded-tool routing, imports), `holdco/agents.py` (prompt sections,
spec rebuild, transcript redaction), `holdco/actions.py` (`policy_action`, `connector_call`, Buffer in
`_social`, labels), `holdco/emailer.py` (Resend sending, `in_reply_to`), `holdco/scheduler.py` (connector checks
and polls in `_housekeeping`), `holdco/doctor.py` (`connector:*` and `vault` rows), `holdco/server.py`
(settings masking and vault routing, `/oauth/callback`, `tag` on approvals, `source` on metrics), `holdco/bus.py`
(redaction), `holdco/web.py` (`HOLDCO_ALLOW_LOCAL`), `holdco/workspace.py` (`.qa`), `holdco/presets.py`,
`holdco/roles.py`, `run.py` (`config.PORT`), `holdco/static/app.js` (connector cards, tag badges,
`connector_call` card, metric sources), `holdco/static/style.css` (card layout), `README.md` (connectors, vault,
browser check), `tests/test_unit.py` (tag integrity), `tests/test_e2e.py`, `tests/fake_ollama.py`.

## Tests

### Fakes

All fakes follow `tests/fake_ollama.py`: a `ThreadingHTTPServer` on `127.0.0.1` with a random port, `.url`,
`.start()`, `.stop()`, and public lists that record what was received.

`tests/fake_mcp_server.py`. Runnable in-process (`FakeMCPHttp(port=0, old=False, sse=False, auth_base=None)`)
and as a script: `python tests/fake_mcp_server.py --stdio [--old]` or `--http PORT [--old] [--sse] [--auth URL]`.
Tools: `echo(text)` → text; `add(a, b)` → structuredContent `{"sum": n}` and text; `fail(text)` → `isError`
true; `image()` → one image content block (a 1x1 PNG); `slow(seconds)`; `input_required()` → a result whose
`resultType` is `"input_required"`. Protocol in the default (new) mode: `server/discover` returns
`{"protocolVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "serverInfo": {...}}`; every request must
carry `_meta["io.modelcontextprotocol/protocolVersion"]` and `_meta["io.modelcontextprotocol/clientCapabilities"]`
and, over HTTP, the `Mcp-Method` and `Mcp-Name` headers (else error `-32600`); a version other than `2026-07-28`
gets `-32022`; every result carries `resultType: "complete"` (except `input_required()`) and
`_meta["io.modelcontextprotocol/serverInfo"]`; `initialize` and `ping` answer `-32601`; no session id is issued.
With `--old`, `server/discover` returns `-32601`, `initialize` answers `protocolVersion "2025-11-25"`, `tools/list`
or `tools/call` before `notifications/initialized` returns `-32600 "not initialized"`, `ping` answers `{}`, and
over HTTP the `initialize` response sets `Mcp-Session-Id` and later requests without it get 400. HTTP: `POST
/mcp`; with `--sse` every response is `text/event-stream` with one `event: message` / `data: <json>` block; with
`--auth`, requests without a bearer token issued by the fake OAuth
server get `401` with `WWW-Authenticate: Bearer resource_metadata="<self>/.well-known/oauth-protected-resource"`,
and `GET /.well-known/oauth-protected-resource` returns `{"resource": "<self>/mcp", "authorization_servers":
["<auth base>"]}`; tokens are validated by `GET <auth base>/introspect?token=`. Records: `calls` (method, params).

`tests/fake_oauth.py`. `FakeOAuth(port=0, expires_in=3600, wrong_iss=False)`: `GET /.well-known/oauth-authorization-server` →
`{issuer, authorization_endpoint, token_endpoint, registration_endpoint, code_challenge_methods_supported:
["S256"], grant_types_supported: ["authorization_code", "refresh_token"]}`; `POST /register` → `{"client_id":
"fake-client"}`; `GET /authorize` validates `client_id`, `redirect_uri`, `state`, `code_challenge`,
`code_challenge_method=S256`, `resource`, stores the challenge and answers `302 Location: <redirect_uri>?code=
<code>&state=<state>&iss=<issuer>` (no human step; with `wrong_iss=True` the `iss` value is a different origin so
the client's issuer check can be exercised); `POST /token` with `grant_type=authorization_code` verifies the
`code_verifier` against the stored challenge (S256) and returns `{"access_token", "token_type": "Bearer",
"expires_in", "refresh_token"}`, and with `grant_type=refresh_token` returns a new pair; `GET /introspect?token=`
→ `{"active": bool}` (a test helper, not part of the flow). Records: `tokens` (issued), `registrations`.

`tests/fake_stripe.py`. `FakeStripe(port=0)`; Bearer auth required: a key containing `_test_` is accepted, any
other key answers `401 {"error": {"message": "Invalid API Key"}}`; form-encoded bodies parsed with
`urllib.parse.parse_qs` and bracket keys flattened into nested dicts. Endpoints (the list the brief names, as
Stripe-shaped JSON objects with `id`, `object`, `created`):

| Path | Behaviour |
|---|---|
| `GET /v1/balance` | `{"object": "balance", "available": [{"amount": 0, "currency": "usd"}], "livemode": false}` |
| `POST /v1/products`, `GET /v1/products/<id>` | stores `{id: prod_<n>, name, description}` |
| `POST /v1/prices` | stores `{id: price_<n>, unit_amount, currency, product, recurring: {interval} or null}` |
| `POST /v1/payment_links`, `GET /v1/payment_links`, `GET /v1/payment_links/<id>` | stores `{id: plink_<n>, url: "<self>/pay/plink_<n>", active: true, line_items: {data: [{price: <price object>, quantity}]}}`; the list returns `{"object": "list", "data": [...]}` |
| `GET /pay/<id>` | an HTML page (200) with a title, an h1 with the product name and a `<form>`; the page the gate's `browser_check` opens |
| `GET /v1/charges` | `{"object": "list", "data": [...]}` honouring a created-date lower bound filter and `limit` |
| `GET /v1/customers`, `GET /v1/subscriptions` | lists; subscriptions carry `status`, `items.data[].price` and `quantity` |
| `POST /v1/refunds` | records and returns `{id: re_<n>, amount, payment_intent}` |
| `POST /_test/charges` (test hook) | body `{"amount": cents, "currency": "usd", "paid": true, "created": epoch?, "customer_email": str?}` → adds a succeeded charge; also available as the Python method `add_charge(...)` |

Records: `requests` (method, path, body).

`tests/fake_resend.py`. `FakeResend(port=0, domains=("pawplan.example",))`: `GET /domains` → `{"data":
[{"name", "status": "verified"}]}`; `POST /emails` (Bearer required) records the JSON body, including
`headers` and `reply_to`, plus the `Idempotency-Key` request header, and returns `{"id": "re_<n>"}`; a repeat
with the same `Idempotency-Key` returns the same id without a second record. Records: `sent`.

`tests/fake_buffer.py`. `FakeBuffer(port=0)`: `POST /` with a JSON `{"query", "variables"}` body: a query
mentioning `account` → account data; `channels` → two channels (`x`, `linkedin`); a mutation mentioning
`createPost` → records and returns an id. Records: `posts`.

### Unit tests (`tests/test_phase1.py`)

`HOLDCO_VAULT_BACKEND=file` and `HOLDCO_ALLOW_LOCAL=127.0.0.1:*` are set before import (the latter is read as a
host wildcard for tests only).

| Class | Tests |
|---|---|
| `TestVault` | put/get/delete/names round trip; a second process with the same `vault.key` reads the value; `HOLDCO_VAULT_PASSPHRASE` derivation works and a wrong passphrase fails the tag check; flipping one ciphertext byte raises `VaultError`; `migrate` moves `brave_api_key` and a company's `smtp_password` to markers and `get_settings`/`company_settings` resolve them; `GET /api/settings` and `_public_company` mask them; writing the mask leaves the secret unchanged and an empty value deletes it; `redact` scrubs a secret from `bus.emit` message and data and from a stored transcript; a secret in a connector tool result never reaches the `audit` table (`actions.audit` redacts `payload` and `result`); a foreign-backend secret raises the "another machine" error |
| `TestMCP` | stdio client against the fake (new generation): `connect`, `list_tools`, `call_tool` text, `isError`, image summary, structuredContent; stdio with `--old`: `protocol == "2025-11-25"` and tools work; HTTP JSON and HTTP SSE responses; the namespaced `_meta` keys on every request and the `Mcp-Method` and `Mcp-Name` headers on every HTTP request in new mode; `Mcp-Session-Id` echoed in old mode only and never sent in new mode; `MCP-Protocol-Version` header present after `initialize` in old mode; an `input_required` result becomes an `ERROR:` text for the agent; a `-32022` reply raises `MCPError("unsupported protocol version")`; 401 raises `AuthRequired` with the metadata URL; a `slow(200)` call raises on the 120 s timeout (patched to 1 s); stderr tail appears in the error when the stdio process exits |
| `TestOAuth` | against `FakeOAuth` and `FakeMCPHttp(auth_base=...)`: `start` returns a URL whose query has `code_challenge_method=S256`, `state` and `resource`; fetching it with a no-redirect opener gives the callback URL; `finish` stores the token set under `oauth.<id>`; an MCP call then succeeds with the bearer token; with `expires_in=1` the next call refreshes; a `TokenSource` refuses a different origin; discovery falls back to `openid-configuration`; the `client_id` from config is used when registration is absent; with `FakeOAuth(wrong_iss=True)` the callback is refused with `OAuthError` and nothing is stored under `oauth.<id>` |
| `TestRiskTags` | every tool in `tools.REGISTRY` has a tag in `RISK_TAGS`; the built-in map matches the table; `connectors.policy_for(tag)` mapping; `irreversible` and `spend_money` are locked (`effective_policy` ignores an `auto` override); `gate(..., policy_action="connector_publish")` queues under that policy |
| `TestConnectors` | every definition has the setup card fields, `health`, and tagged tools; `POST /api/connectors` creates an instance, puts the secret in the vault and runs health; a failing health gives the Doctor row `connector:<id>`, one `notify.send` per day, and the "Connectors that are down right now" line in `system_prompt`; `poll_metrics` writes rows with `source` and the `<key>.<metric>` name; old metric rows read `source='manual'`; `DELETE` removes the vault secrets |
| `TestFindTool` | catalog from an `api` definition and an MCP fake; scoring returns the expected top match; loads 3 into `ctx.loaded_tools`; `agents.run` (run_script) sends the loaded spec on the next step; `tools.execute` routes a loaded name; a read tool runs free and logs a `tool` event; a publish tool is queued and, after `decide(approve)`, executed and audited; an unclassified MCP tool asks; a role outside `allow` is refused |
| `TestStripe` | `create_payment_link` against `FakeStripe` creates product, price and link and names the URL; `list`/`get`; `recent_payments` after `add_charge`; `create_refund` is tagged irreversible and queued; metrics computed from charges and subscriptions (`mrr_usd` normalises a yearly price); mode from the key; health bad on a live-looking key (401) |
| `TestResend` | `emailer.send` posts to the fake when `email_provider="resend"` or `auto` with a healthy instance, and uses SMTP otherwise; the posted body carries `headers` with Holdco's `Message-ID` and an `Idempotency-Key`; the `emails` row is written with `message_id` and Resend's `id` in `uid`; `poll_inbox` fills `in_reply_to` from a crafted message (patched IMAP); `email.replies_7d` matches by `in_reply_to` and by subject fallback |
| `TestBuffer` | `list_channels` and `create_post` against the fake; `social_post` executor posts through Buffer when configured and falls back to the webhook otherwise |
| `TestPlaywright` | health returns warn with "npx not found" when `shutil.which` is patched to None; the classification map tags the named tools; `browser_evaluate` and `browser_run_code_unsafe` never appear in `find_tool` results and `connectors.call` refuses them for every role, including `engineering`; `browser_file_upload` queues under `connector_publish`; `find_tool`-loaded `browser_navigate` to `127.0.0.1` is refused |
| `TestBrowserCheck` | static fallback on `site/index.html` with no server: first line "Static check only", audit included, a broken same-site link reported, a form without a target reported; with a local `ThreadingHTTPServer` as the preview: the status line; an external URL (fake Stripe `/pay/...` under `HOLDCO_ALLOW_LOCAL`) reports 200, the title and "form found" |

The independent review's findings each have a regression test in `tests/test_phase1_review.py` (connector
identity at execution, browser landing checks, Stripe MCP tags, the browser lock and page binding, test-only
hidden fields, secret-only marker resolution, bounded MCP reads, the stdio environment, the first-start timeout,
refund currency and idempotency, test-mode revenue, Buffer default channels, and a keyring outage).

### E2E additions (`tests/test_e2e.py`)

`setUpClass` additionally starts `FakeStripe`, `FakeResend` and `FakeBuffer` and launches Holdco with
`HOLDCO_VAULT_BACKEND=file`, `HOLDCO_ALLOW_LOCAL=127.0.0.1:*`, `HOLDCO_METRICS_POLL_SECONDS=1`,
`HOLDCO_CONNECTOR_CHECK_SECONDS=5`. `tests/fake_ollama.py` gets a script for `Forge` when the task title is
"Create a payment link for the Starter plan" (see the gate walkthrough) and a `Harbor` script that replies
through `send_email` when Resend is configured.

- `test_21_connectors_api_and_vault`: `POST /api/connectors` (stripe, company scope, a `_test_` key, `api_base`
  = fake) → health ok and mode `test`; `GET /api/connectors` masks `api_key`; the data folder has `vault.json`
  and no plain key in `holdco.db` (open it with `sqlite3` and search the settings and companies tables); the
  Doctor shows `connector:<id>` ok.
- `test_22_stripe_gate`: the walkthrough below.
- `test_23_resend_and_buffer`: configure both; approve Hunter's pending email → `FakeResend.sent` has it and the
  `emails` row exists; approve a social post → `FakeBuffer.posts` has it; the audit log has both rows.
- `test_24_mcp_oauth_round_trip`: register a test-only `mcp-http` definition pointing at `FakeMCPHttp(auth_base=
  FakeOAuth)` (the definition is added by the test through `connectors.DEFINITIONS` only when `HOLDCO_TEST_MCP`
  is set in the app's environment); `POST /api/connectors/<id>/oauth/start` → open the URL with urllib (it
  redirects to the real dashboard `/oauth/callback`) → the health check turns ok and `find_tool("echo")` loads
  `fakemcp__echo`.

### Gate test as a scenario walkthrough (`test_22_stripe_gate`)

Setup: the fake model, Holdco, and `FakeStripe` are running; PawPlan was founded in `test_03`; the Stripe
instance from `test_21` is healthy in test mode; the company's `connector_publish` policy is set to `auto` for
this test so the run can finish with the link in hand (the alternative path, approving the `connector_call`
request from the Inbox, is covered by `TestFindTool`).

1. The test posts a task for Forge (engineering): title "Create a payment link for the Starter plan",
   description "Use the Stripe connector. $19 per month.", deliverable "Stripe payment link (URL in the handoff)",
   acceptance "- the link is live in Stripe test mode\n- browser_check on the link returns 200".
2. The fake model's Forge script for that title: `find_tool(query="stripe payment link")` → the result lists
   `stripe__create_payment_link [publish]` first and loads it; `stripe__create_payment_link(name="Starter plan",
   amount_usd=19, recurring_interval="month")` → `connectors.call` → tag `publish` → `gate` with
   `policy_action="connector_publish"` → policy `auto` → `connectors.execute` → `FakeStripe` receives
   `POST /v1/products`, `POST /v1/prices`, `POST /v1/payment_links` → the tool result names
   `http://127.0.0.1:<fake>/pay/plink_1`; the `audit` table gets a `connector_call` row with
   `approved_by='policy:auto'` and the payload's `tag='publish'`.
3. `browser_check(path_or_url="http://127.0.0.1:<fake>/pay/plink_1")` → Playwright is not set up → the external
   branch under `HOLDCO_ALLOW_LOCAL` → "Static check only: Playwright is not set up", "-> 200", the title and
   "form found".
4. `finish(summary="Starter plan payment link created", status="done", deliverable="http://127.0.0.1:<fake>/pay/
   plink_1", evidence="stripe__create_payment_link returned the link; browser_check returned 200")`. The
   deliverable check in `tools.finish` only inspects file-like deliverables, so a URL passes. The task is `done`
   and `tasks.result` contains the link.
5. The test asserts: the task is `done`; the run transcript has `find_tool`, `stripe__create_payment_link` and
   `browser_check` tool messages in that order; `FakeStripe.requests` has the three POSTs; the audit row exists;
   `stripe__get_payment_link` run by the test through `connectors.execute` (or by the CEO's next cycle, which
   the fake script makes it call when a done task's deliverable starts with `http`) returns `active: true`.
6. The test injects a paid charge: `FakeStripe.add_charge(amount=1900, currency="usd", paid=True)`.
7. Within `HOLDCO_METRICS_POLL_SECONDS` (1 s in tests) plus one tick, `connectors.poll_metrics` runs:
   `GET /api/companies/<cid>/metrics` contains a row `name="stripe.revenue_usd_30d"`, `value=19.0`,
   `source="stripe"`, `agent_id=None`, and rows for `stripe.payments_30d` (1), `stripe.customers`,
   `stripe.mrr_usd` (0, no subscriptions).
8. The test asserts that no run transcript in the company contains a `record_metric` call with a name starting
   `stripe.` and that the Overview tile data (`GET /api/companies/<cid>`) lists the metric with `source`.

That is the plan's gate: payment link created by an agent, opened by a browser check, a test purchase showing
up as revenue with nobody typing it.

## Gate

| Gate clause (plan and brief) | Proof in the suite |
|---|---|
| An agent creates a payment link in Stripe test mode | `test_22` steps 1-2, `TestStripe` |
| A browser check confirms it opens | `test_22` step 3 (static fallback, labelled), `TestBrowserCheck` |
| A test purchase shows up as revenue without anyone typing it | `test_22` steps 6-8, `TestConnectors` (`source`) |
| The fake MCP server proves negotiation, tools/list, tools/call, SSE and the 401 → OAuth path | `TestMCP`, `TestOAuth`, `test_24` |
| Secrets leave the plain-text tables and never reach transcripts | `TestVault`, `test_21` |
| Risk tags drive approvals; unclassified asks; spend and irreversible are locked | `TestRiskTags`, `TestFindTool`, `TestStripe` (refund) |
| Full suite green on Windows and Linux | CI matrix; `fake_mcp_server --stdio` is launched with `sys.executable`, so no Node is needed in CI |

## How to try it on your PC

1. Check out `claude/phase-1-connect`, start Holdco. On first start the console prints "Vault: dpapi" (Windows)
   or "Vault: secret-service" / "Vault: file" (Linux) and that existing keys were moved. Secret fields in
   Settings now show the mask.
2. Stripe: in the Stripe dashboard switch to test mode and create a restricted key with the Agent tag (the
   hosted MCP server requires it from December 2026, so migrate by November 30, 2026; verify the dashboard
   steps). In the company's Settings → Connectors → Stripe
   paste the key, press `Save and test`: the card shows "ok, test mode" and the polled metrics (all zero).
3. Chat to the CEO: "Create a payment link for the Starter plan at $19 a month." The engineer's request
   "Connected service call: stripe create_payment_link [publish]" lands in the Inbox (and on the phone); approve
   it. The handoff names the link. Open it, pay with one of Stripe's documented test card numbers (verify), then
   press `Test` on the Stripe card or wait for the hourly poll: `stripe.revenue_usd_30d` appears on the Overview
   with source `stripe`.
4. Playwright: install Node.js, then on the global Settings page enable the Playwright card and press `Test`
   (the first run downloads a browser, allow a minute). Ask QA in chat to "run browser_check on site/index.html";
   the Files tab shows `.qa/index.png` and `.qa/index-mobile.png`, and the report lists console errors and link
   results.
5. Resend: create an API key and verify your sending domain in Resend, fill the card, set the company's
   `email_provider` to `resend`, approve a sales email; it arrives through Resend and `email.sent_7d` ticks.
6. Buffer: create a personal access token, fill the card, approve a social post; it appears in your Buffer queue.
7. Brave: paste the key on the Brave card; the Doctor `search` row shows `brave: ok`.
8. Break something: revoke the Stripe key. Within an hour the Doctor row turns red, one phone message arrives,
   and the next planning cycle's prompt lists Stripe under "Connectors that are down right now".

## Open questions

- Preview authentication: when `HOLDCO_PASSWORD` is set, the preview URL needs Basic auth and Playwright's
  `browser_navigate` cannot send it. Options: exempt `/preview/` for requests from 127.0.0.1, or a one-time token
  query parameter minted by `browser_check`. The exemption is simpler; decide before Phase 3 makes
  `browser_check` a gate.
- `HOLDCO_ALLOW_LOCAL` is a test-only hole in the private-URL rule. It is read once at import and documented, but
  a reviewer may prefer the fakes to be reachable through a hostname that resolves publicly. No such hostname is
  available offline, so this document keeps the env var.
- Playwright MCP on Windows: `npx.cmd` resolution and the first-run browser download inside the health check's
  120 s. If the download regularly exceeds it, make the first health check asynchronous and show "installing".
- Buffer's GraphQL API is in beta; the account and channel queries are placeholders until verified, and the
  connector should ship disabled by default with a "beta" note on the card.
