# Testing strategy

Holdco is tested without a GPU, a network connection or a pip install. A scripted fake stands in for every
external service, starting with the fake Ollama server that already drives the whole application end to end.
Each phase of [plan.md](plan.md) adds a unit-level test module, extends the end-to-end suite, adds the fakes it
needs, and turns its gate into assertions the suite proves on every push. Holdco Bench, built in Phase 3, is a
different instrument: it measures a real model on the owner's GPU, and CI only checks that the Bench itself
works. What no fake can prove is listed at the end as manual checks on the owner's PC.

Status: the "What exists today" section describes the current suite (47 tests on `main`). Everything else is
design, built phase by phase (see `phases/`). See [architecture.md](architecture.md) for the modules under test
and [reliability.md](reliability.md) for the Bench scenarios in detail.

## What exists today

### Unit suite: `tests/test_unit.py`

Runs in-process against a temporary data folder (`HOLDCO_DATA` is set to a `tempfile.mkdtemp` folder before
`holdco` is imported, then `db.init()` runs once). Two fixtures do most of the work: `make_company(name,
members)` inserts an active company and hires presets by key; `ctx_for(company, role, task, kind)` builds a
`tools.Ctx` with the role's real tool belt, so tools are called exactly as `agents.run` calls them.

| Class | Covers |
|---|---|
| `TestChecks` | `check_contrast` ratios, `audit_page` on a bad and a good page, `review_text` rules |
| `TestModelClient` | `extract_json`, thinking-tag splitting, text tool-call parsing, per-model sampling, message conversion for Ollama, OpenAI and JSON mode, tool-parse-error retries |
| `TestPresets` | Preset integrity (every tool in a belt exists, routines well formed), templates, old agents resolving to presets |
| `TestWorkspaceAndWeb` | `safe_path` escapes, private URL blocking, HTML to text, the DuckDuckGo parser |
| `TestTools` | Tool belts and aliases, task dedupe, the deliverable check in `finish`, hiring, checks as tools, the approvals flow (queue, dedupe, approve with edits, reject with feedback creating a "Revise:" task, failed email retried), purchases never auto-approved |
| `TestLoopGuard` | Re-checking a file after rewriting it is allowed; identical repeats and identical writes are still stopped; a failed write does not reset the guard |

### End-to-end suite: `tests/test_e2e.py`

`EndToEnd.setUpClass` starts a `FakeModelServer` and a `HoldcoProcess`, then points the app at the fake with
`PUT /api/settings`. The 15 tests are numbered and run in order against one living server: dashboard and model
listing, API security (`X-Holdco` header, JSON only, short ideas rejected), founding from the `launch` template
through to five done tasks and five pending approvals of five kinds, files and the sandboxed preview, approvals
(approve with an edit, a failing email retried, a rejection creating a revise task, `run_code` really running),
chat, QA review with the deliverable check pushing back a lazy finish, recurring duties (and duties that wait
for email), pausing, hiring and settings (secrets masked, locked policy ignored), restart recovery (founding
resumes without duplicate tasks), the prompt-based tool fallback, the OpenAI-compatible backend, the Thinking
events, and the feed plus deletion. `tearDown` fails any test after which the server log contains a
`Traceback`, so unhandled exceptions anywhere in the process are caught.

### The fake model server: `tests/fake_ollama.py`

`FakeModelServer(port=0, delay=0.05)` is a `ThreadingHTTPServer` on `127.0.0.1` that answers `GET /api/tags`,
`GET /v1/models`, `POST /api/chat` and `POST /v1/chat/completions`. `script(messages, tools)` decides the reply
from the prompt: the first line of the user prompt picks founding, a planning cycle or chat; otherwise the agent
name at the start of the system prompt picks a per-role sequence (Forge builds the site and deploys, Echo
drafts and posts, Hunter saves leads and emails, Scout searches and remembers, Ledger records a metric, asks
for a purchase and runs code, Sentry audits and files a fix task, Repeater loops). The step index is the number
of assistant turns so far, so a script is a list of tool calls indexed by step. Three model names select
behaviour: `gpt-oss:20b` answers with native tool calls and a `thinking` field, `notools:latest` rejects
`tools` with HTTP 400 so the client falls back to prompt-based JSON calls, and `local-model` is served on the
OpenAI-compatible path. The server records every call (`path`, `model`, `think`, `options`, `ntools`) so tests
can assert what the app sent, and `delay` is adjustable mid-test (the restart test raises it). Run
`python tests/fake_ollama.py 11999` to click around the dashboard without a GPU.

### Helpers: `tests/helpers.py`

`free_port()`, `wait_for(predicate, timeout, interval, what)` (polls and raises a clear `AssertionError`),
`Client` (a tiny JSON HTTP client that adds `X-Holdco: 1` on writes and bypasses proxies) and `HoldcoProcess`
(runs `python run.py --no-browser --port N` with `HOLDCO_DATA`, `HOLDCO_PORT`, `HOLDCO_TICK_SECONDS=1`,
`HOLDCO_ROUTINE_CHECK_SECONDS=1`, logs to `server.log` in the data folder, `start()`, `stop()`, `log()`).

### CI

`.github/workflows/tests.yml` runs on every push and pull request: `ubuntu-latest` and `windows-latest`, Python
3.10, 3.12 and 3.13, 15-minute timeout, no install step. The command is the one below.

### How to run

```
python -m unittest discover -s tests -t . -v
```

Run one module with `python -m unittest tests.test_unit -v`, one test with
`python -m unittest tests.test_e2e.EndToEnd.test_05_approvals -v` (the e2e tests depend on the earlier ones
in the class, so run the class when in doubt).

## Principles

1. No network, no GPU, no pip. A test that needs any of them is not a test; it is a manual check (last
   section). Optional imports such as `ddgs` are never required.
2. One fake per external service, in `tests/` (from Phase 3 the scripted Stripe, Telegram, MCP and search
   servers live in `holdco/bench/mocks.py` and the `tests/fake_*.py` files import them), built on the
   `fake_ollama.py` pattern: a `ThreadingHTTPServer`
   (or a subprocess for stdio) with scripted behaviour, a `calls` or `sent` list the test can inspect, modes the
   test can switch at runtime, and a `start()`/`stop()` pair. Holdco reaches every fake through a base-URL
   override of the same kind as `telegram_api_base`; each phase document names its overrides.
3. Scripted behaviour is keyed on prompt content, never on call counts alone. The fake model recognises the
   kind of run from the user prompt and the role from the system prompt, so a change in prompt wording that
   breaks the script is a test failure worth seeing.
4. Every phase adds `tests/test_phaseN.py` (in-process, fakes started in `setUpClass`) and extends
   `tests/test_e2e.py` with numbered tests that drive the real server over HTTP. The e2e file stays one
   ordered class so a single founded company is reused.
5. Gates are tests. Each phase's gate from the plan is written as assertions before the phase is built, and the
   phase is not done until they pass on both operating systems.
6. Tests prove the guardrail, not the model. Where behaviour depends on the model (the kill switch stopping a
   run, taint forcing an approval, a lease refusing a write), the fake model is scripted to attempt the wrong
   thing and the test asserts that the code refused it.
7. Fakes fail realistically. Every fake has at least one scripted failure mode (an outage, a 401, a
   bot-challenge page, a malformed reply) so retries, cooldowns and escalations are exercised.
8. A test never sleeps for a fixed time when it can `wait_for` a condition. The e2e server runs with a 1-second
   tick so "within the next scheduler tick" means a few seconds, not minutes.
9. Secrets in fixtures are built at runtime (as `FAKE_KEY` is today) so secret scanners do not flag the repo.
10. The server log is part of every e2e assertion: any `Traceback` fails the test.

## The fakes planned per phase

| Phase | Fake | Stands in for | Must implement (per the brief) |
|---|---|---|---|
| 0 | `tests/fake_telegram.py` | Telegram Bot API | `getMe`; sending a message with HTML parse mode and an inline keyboard; `getUpdates` long polling that returns queued updates at once and otherwise waits up to its timeout; a test hook to inject a callback query (`a:<approval_id>:approve` or `reject`, `q:<approval_id>:<option index>`) and a text command (`/status`, `/stop`, `/resume`) from the configured chat id, and from a foreign chat id that must be ignored; answering a callback; editing a message to show the outcome. Records every message sent (chat id, text, buttons), every callback answered and every edit. (verify against the vendor docs before connecting) |
| 1 | `tests/fake_stripe.py` | Stripe REST API | `/v1/balance` (health), products, prices, payment links, charges, subscriptions; serves a payment page at each link's URL so a static `browser_check` gets HTTP 200; accepts a test-prefixed key and rejects others with 401; a hook for the test to inject a paid charge and an active subscription so the metrics poll produces `stripe.revenue_usd_30d`, `stripe.payments_30d`, `stripe.customers` and `stripe.mrr_usd`. (verify against the vendor docs before connecting) |
| 1 | `tests/fake_resend.py` | Resend API | `GET /domains` (health) and `POST /emails` (send; returns an id; records recipient, subject, body and sender); a mode that returns an error so the `send_email` executor reports `FAILED:` and the approval is retryable. (verify against the vendor docs before connecting) |
| 1 | `tests/fake_buffer.py` | Buffer GraphQL API | One GraphQL endpoint: an account query for health, listing channels, creating a post (records text, channel ids, link, schedule); 401 for a bad token. (verify against the vendor docs before connecting) |
| 1 | `tests/fake_mcp_server.py` | Any MCP server (and the hosted Stripe and Playwright servers in tests) | Runs as a stdio subprocess (newline-delimited JSON-RPC frames, some noise on stderr) and as an HTTP server (Streamable HTTP, replies as JSON or as SSE `data:` lines selected per mode). Protocol generations selectable by mode: new (answers `server/discover` with its protocol versions, capabilities and identity; requires the namespaced `_meta` keys `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` on every request and, over HTTP, the `Mcp-Method` and `Mcp-Name` headers, else -32600; answers -32022 to an unknown version; returns `resultType` and `serverInfo` in every result, including one tool that answers `input_required`) or old (returns -32601 to `server/discover`, accepts `initialize` with `2025-11-25` and `notifications/initialized`, may answer with an older version, issues `Mcp-Session-Id` and then requires it). `tools/list` and `tools/call` with a few tools: one returning text, one returning `isError`, one slow enough to hit the 120 s call timeout when the test lowers it, one returning an image block to exercise the summary. An auth mode that answers 401 with `WWW-Authenticate` carrying a resource metadata URL until a bearer token from `fake_oauth` arrives |
| 1 | `tests/fake_oauth.py` | An OAuth authorization server plus the protected resource metadata | Protected resource metadata (RFC 9728) and authorization server metadata (RFC 8414), dynamic client registration (RFC 7591), an authorization endpoint that redirects straight back to `http://127.0.0.1:<port>/oauth/callback` with a code and the state (no login page), a token endpoint that verifies the PKCE S256 verifier and issues short-lived access tokens with a refresh token, and the refresh grant. Records every token issued and every resource request's bearer token so the test can prove tokens go only to their issuer's resource |
| 3 | `holdco/bench/mocks.py` | Stripe, Telegram, MCP and search inside Bench scenarios | From Phase 3 the scripted servers live here, shipped with the package so the Bench runs without the tests folder, with scripted failures per scenario (a blocked search provider, a tool error, an unreachable connector); `tests/fake_stripe.py`, `fake_telegram.py` and `fake_mcp_server.py` become thin imports of it |

Phase 2 adds no fake: its tests reuse `fake_stripe.py` for connector metrics and the fake model for brain and
rhythm scripts. Phase 4 adds one fake per new connector on the same pattern; the names are fixed in
`phases/phase-4-scale.md`.

The fake model grows with each phase. Its `script()` gains branches for the new prompts (an escalation answer in
the task prompt, a brain digest in the system prompt, a decision tool, a payment-link task) and new scripted
roles (an analyst writing a brief from provided data, a CEO calling `set_goal` and `ask_decision`). Scripts
must call tools with the arguments the brief fixes, so argument names are tested too.

## Phase gates as tests

### Phase 0: Stabilize (`tests/test_phase0.py`, e2e additions)

Gate from the plan: a new company founds with no help from the owner, and any failure reaches the phone within
5 minutes as one clear question.

| Check | How the suite proves it |
|---|---|
| Founding needs no help | e2e: found from a template against the fake model; the company becomes `active`, no `question` approval exists afterwards, and the Doctor preflight returned ok (`model_server` and `model` rows ok) |
| Preflight refuses a broken dependency | e2e: point `llm_base_url` at a closed port; `POST /api/companies` returns 409 with `checks`; with `force: true` it founds anyway. With the fake search chain fully blocked, founding proceeds and the founding prompt tells the CEO search is down |
| Each failure produces exactly one question and one Telegram message with buttons | Four scenarios, each asserting one pending `approvals` row with `action == "question"` and the right `pattern` in its payload, and exactly one message in `fake_telegram` with option buttons, within the next tick: a founding that ends `partial` (the fake CEO finishes partial), a task failing twice (the fake agent errors twice; `max_task_attempts` 2), a search outage (every provider returns a challenge page; one error event per 30 minutes, not per call), a model server outage with open tasks and autopilot on (the `stalled` pattern after the 2 h window, moved by editing timestamps in the database as the recurring-duties test does today) |
| Pressing a button answers it | Inject a callback `q:<id>:<index>` into `fake_telegram`; the approval becomes `answered`, the pattern action ran (a task cancelled, a company paused, founding restarted), the callback was answered and the message edited. A callback from a foreign chat id changes nothing |
| Dedupe | A second identical failure within 6 h creates no second question; a different pattern does |
| Working notes and retry-differently | Unit: `note_progress` appends and trims to 4000 chars; a run ending `partial` adds the automatic entry (attempt, how it ended, files written, last 3 calls, last error); the second attempt's prompt contains "Attempt 1 failed because" and the guidance for its failure kind; after `max_task_attempts` the task is `blocked` and escalated, not retried |
| Search fallback chain | Unit with a fake search provider server: the configured provider is tried first, a challenge page marks `web.PROVIDER_STATUS[name]` with a 10-minute cooldown, the next provider answers, `search()` records which one, and all failing raises `WebError` naming each provider's reason |
| Kill switch | e2e: `POST /api/kill {"on": true}` while a run is in progress: the run ends `stopped`, nothing new dispatches, an `auto` action queues instead of executing, `POST /api/approvals/<id>` returns 400, `/status` and `/resume` over fake Telegram still work; `scheduler_enabled` is unchanged |
| Backups | Unit: `run_backup()` writes `data/backups/holdco-*.zip` containing a consistent `holdco.db`, `companies/` without `.runs`, `.trash`, `__pycache__`, `node_modules`; only `backup_keep` newest remain; `GET /api/backups` lists them; the Doctor backup row turns ok |
| Audit log | Every executed outbound action (auto or approved) adds one `audit` row with `proposed_by`, `approved_by` and the exact payload; `GET /api/audit` returns them; there is no route that updates or deletes one |
| Doctor | `GET /api/doctor` returns every row kind with a status and a fix; `deep=true` runs the tool-call ping and a live search against the fakes; the disk and errors rows use thresholds from the brief |
| Notifications | `POST /api/notify/test` sends to fake Telegram and a fake ntfy endpoint; identical text within 1 h is sent once; `notify_level` filters |

Full suite green on Windows and Linux is part of the gate.

### Phase 1: Connect (`tests/test_phase1.py`, e2e additions)

Gate: in Stripe test mode, an agent creates a payment link, a browser check confirms it opens, and a test
purchase shows up as revenue without anyone typing it.

| Check | How the suite proves it |
|---|---|
| The gate scenario | e2e with `fake_stripe`: configure the stripe connector with a test key; a task "Create a payment link for the Starter plan" for engineering; the fake Forge calls `find_tool("payment link")`, then `stripe__create_payment_link`; `connector_publish` is set to `auto` for this test so the run finishes with the link in hand (the Inbox approval path is covered by `TestFindTool`), and the `audit` row has `approved_by='policy:auto'`; the handoff names the link; `browser_check` on the link returns a result that starts with "Static check only" and reports 200; the test injects a paid charge; after the next `poll_metrics()` a `metrics` row `stripe.revenue_usd_30d` with `source == "stripe"` exists and no `record_metric` call appears in the transcript |
| MCP client, both generations and transports | `fake_mcp_server` in four modes (stdio and HTTP, new and old generation): `connect()` sets `client.protocol` to the generation in use, `list_tools()` returns the tools, `call_tool()` returns text, `isError` is passed through, an SSE reply is parsed to the message with our id, `Mcp-Method` and `Mcp-Name` are sent on every HTTP request in new mode, `Mcp-Session-Id` is echoed in old mode only, an `input_required` result becomes an `ERROR:` text for the agent, the connect and call timeouts raise clear errors, stderr noise does not break framing |
| OAuth path | HTTP mode with auth: the first call raises `AuthRequired` with the resource metadata URL; `POST /api/connectors/<id>/oauth/start` returns the authorization URL; following it through `fake_oauth` lands on `GET /oauth/callback`; the token is in the vault under `oauth.<connector id>`; the next call succeeds; the access token is never sent to a second fake resource with a different issuer; refresh happens when under 60 s remain |
| Vault | Each backend that can run on the CI host is tested (`file` everywhere; `dpapi` on Windows; `secret-service` skipped unless `secret-tool` works): `put/get/delete/names`; the file backend's tag rejects a tampered byte; `HOLDCO_VAULT_PASSPHRASE` derives the key; startup migration moves `llm_api_key`, `brave_api_key`, `telegram_bot_token`, `smtp_password`, `imap_password`, `netlify_token` into the vault and leaves `vault:<name>` markers; the API still masks; `redact()` scrubs a known secret from `bus.emit` messages and data and from stored transcripts |
| Risk tags and policies | Every tool in `tools.REGISTRY` has a tag from `config.RISK_TAGS`; the tag-to-policy mapping in the brief holds for each connector tool; an unclassified MCP tool queues under `connector_publish`; `irreversible` cannot be set to `auto` through the API |
| Small tool belts | A role's prompt never lists connector tools until `find_tool` loads them; `find_tool` returns at most 5 matches and loads the top 3 into `ctx.loaded_tools`, named `<connector key>__<tool>`; loaded tools route through `connectors.call()` and produce `connector_call` approvals and audit rows |
| Health and metrics | `POST /api/connectors` runs the health check and returns it; the scheduler's hourly check marks a failing instance; the Doctor shows it; the owner is alerted once per day; the system prompt lists it under "Connectors that are down right now" |
| Resend and Buffer | With `fake_resend` configured and `email_provider` auto, an approved email goes through `POST /emails` instead of SMTP and is logged in `emails`; inbound stays on IMAP. With `fake_buffer`, an approved `social_post` creates a post through the GraphQL endpoint; without it the webhook or the manual link is used as today |
| browser_check | Static fallback on CI (no Playwright): the first line says so; it reports status, title, h1, forms and same-site links for a `site/` path and an external URL. The Playwright path is a manual check |

### Phase 2: Organize (`tests/test_phase2.py`, e2e additions)

Gate: every open task links to a goal, and the weekly review is built from connector data rather than agents'
summaries.

| Check | How the suite proves it |
|---|---|
| Tasks link to goals | With active goals, a CEO `create_task` without `goal_id` returns a warning and the task is listed in the weekly review; the weekly review cancels tasks with no goal; after it, every open task has a `goal_id`; `set_goal` refuses a fourth active goal |
| Weekly review from connector data | Seed `metrics` rows with `source == "stripe"` from `fake_stripe` and a few `manual` rows; the review's `briefs.data` JSON contains the connector values and none of the agent-typed ones for the same names; the fake CEO's narrative is written from that data (the prompt contains it) |
| Brain | `digest()` stays under 6000 chars with a full brain; `propose_brain_change` creates a pending `brain_changes` row; a price or ICP change becomes a `decision` approval for the owner instead; `review_brain_change` applies an accepted one; founding writes `strategy` and the first goals |
| Dependencies and leases | A task with `depends_on` is not dispatched until those are done or cancelled; a `write_file` to a path leased by another run returns the `ERROR` from the brief; the lease is released at run end and expires after 30 min |
| Rhythms | Routine tasks for the daily plan, daily brief, weekly review and monthly check are created at the right cadence (timestamps edited as in the recurring-duties test); the daily brief lands in `briefs`, is sent to fake Telegram and is returned for the Portfolio page |
| Decisions | `ask_decision` creates a `decision` approval with options and a deadline; the scheduler auto-resolves a reversible one at its deadline with the recommended option (`decided_by == "deadline"`) and notifies; an irreversible one waits; the Inbox and Telegram render option buttons |
| Budgets | A company over `gpu_hours_per_day` (run durations edited in the database) is skipped for task dispatch but its routine tasks still queue; task runs use `min(steps_per_task, task.budget_steps)`; `social_post` refuses past `posts_per_week`; purchases stay locked |

### Phase 3: Verify (`tests/test_phase3.py`, e2e additions)

Gate: the Bench runs in CI against the fake model and on the owner's GPU against real ones, and no site task
can be marked done without passing the browser check.

| Check | How the suite proves it |
|---|---|
| Bench runs in CI | `python -m holdco.bench --model gpt-oss:20b --json` against the fake model passes every scenario; the lazy mode fails exactly the scenarios that test the behaviour it lacks (see Holdco Bench below); `POST /api/bench/run` then `GET /api/bench/results` shows a `bench_runs` row with the four scores |
| No site task done without the browser check | A task whose deliverable is under `site/`: the fake agent writes a page with a form that has no target and calls `finish(done)`; the first reply is "Not finished yet: ..." naming the browser check failure; the second forces `partial`; a fixed page passes; on CI the static fallback is used and labelled in the gate result; the CEO's review prompt contains the gate result |
| The other gate kinds | Code: a deliverable under `code/` with a failing test is not done; a passing one is. Payment link: a handoff naming a link that `fake_stripe` reports inactive is not done. Outreach: an email without an opt-out line, over the daily cap, to a suppressed address, or to a flagged EU contact without consent is not done. Research: a file under `research/` citing a URL that was not fetched in this task is not done |
| Taint forces approval | A run that called `fetch_url` then `social_post` under policy `auto` queues the post, and the Inbox card says it was asked because the session read outside content; a run that only read its own files executes under `auto` |
| Trust ladder promotes and demotes | 15 approvals without edits raise `trust.streak` and create a promotion decision; accepting it sets level 1 and the next action runs auto until `cap_per_day`, then asks; an edit resets the streak; a rejection demotes; `spend_money`, `irreversible`, a legal-sounding email and a first message to an unknown contact never promote |
| Structured tool calls and repair | In JSON mode on the Ollama backend the request carries a `format` schema whose tool enum is the belt; `_clean_args` returns the precise repair messages from the brief for a wrong type and an unknown argument |
| Sandbox | With docker absent (CI): `run_code` behaves as today, the Doctor shows the warning row, the approval card says it runs unsandboxed. The container path is a manual check |
| Model routing and context | `llm.pick_model(job)` returns `small_model` for triage, summary and classify when set; memory search with a fake `/api/embed` endpoint returns the nearest note and falls back to FTS without one; `runs.max_prompt_tokens` is recorded and Settings reports the p95 warning |
| Dispatch priorities | Follow-ups to fresh decisions run before due tasks before p1 before routines; a low-value research task waits outside `quiet_hours` when anything else is queued |

### Phase 4: Scale (`tests/test_phase4.py`, e2e additions)

Gate: the owner can run the portfolio for a week from the daily brief and the phone alone. The suite proves the
mechanical parts; the week itself is a manual check.

| Check | How the suite proves it |
|---|---|
| Everything reaches Telegram and can be answered there | Over one scripted "day": an approval, a decision, an escalation and the daily brief each arrive in `fake_telegram`; each of the first three is answered by an injected callback and the answer is applied |
| Holdco as an MCP server | `mcp.Client` connects to the running test server's `/mcp` (HTTP, both generations) and to `python -m holdco.mcp_server` (stdio): `list_companies`, `get_company_brain`, `list_inbox`, `decide`, `create_task`, `get_metrics`, `latest_brief`, `doctor` return the same data as the JSON API; with `HOLDCO_PASSWORD` set, an unauthenticated `/mcp` call is refused |
| Portfolio allocation | Two companies with seeded metrics and run durations get different allocation scores; the one with traction gets the first free slot; the other still gets at least one run per cycle; the monthly portfolio review writes a `briefs` row of kind `portfolio` and a decision per company |
| New connectors | Each connector's health, metrics and tools are tested against its fake: porkbun availability and pricing as `read`, registration as `spend` plus `irreversible` with dry run on by default; cloudflare deploy as `publish` through the `deploy_site` executor; github read-only tools for every role and write tools only for engineering; plausible and umami metrics `analytics.visitors`, `analytics.visits`, `analytics.conversions`; forms polled into `forms.signups` and CRM contacts with consent and source; n8n webhook and MCP; comfyui, ads and accounting shown as not available with a reason |

## Holdco Bench

The test suite proves that Holdco's code does the right thing when the model behaves as scripted. The Bench
answers a different question: does this real model, on this GPU, behave well enough on Holdco's own work? It
runs in `holdco/bench/` (Phase 3) and is nondeterministic by nature, so its result is a score, not a pass or
fail.

| | Test suite | Holdco Bench |
|---|---|---|
| Model | `fake_ollama.py`, scripted | A real model through Ollama (or the fake, in CI) |
| Connectors | Fakes with scripted failures | `bench/mocks.py`, the same fakes wired per scenario |
| Outcome | Assertions, binary | Pass rate, tool-call validity (valid tool calls over model replies), tokens per second, steps per scenario |
| Where | CI and the owner's PC, no GPU | The owner's GPU: `python -m holdco.bench --model X [--only name] [--json]`, or Settings "Test this model", or `POST /api/bench/run`; results in `bench_runs` and on `#/bench` |
| Purpose | Catch regressions in Holdco | Pick models by evidence; gate prompt and preset changes |

A scenario is a seeded company state (brain, tasks, files), one run to perform (kind, role, task), the mock
connectors and their scripted failures, and automatic assertions: a tool was called with valid arguments, a
file exists and passes a check, the finish status, no repeated call, an approval respected, an email compliant,
an escalation raised, a claim cites a fetched URL. About 30 scenarios cover the plan's list (found a company
from an idea, research with a blocked provider, fix a page that fails QA, handle a tool error, do not repeat a
failed call, respect an approval, write a compliant cold email) plus scenarios per model job (triage, summary,
classify) so the owner can see whether `small_model` is good enough for each.

CI runs the Bench twice. Against the fake model in its normal mode, every scenario must pass: this proves the
runner, the seeding, the mocks and the assertions work. Against the fake model in a "lazy" mode, which finishes
without doing the work, repeats a failed call, ignores an approval refusal, writes emails without an opt-out
line and cites nothing, the scenarios that test those behaviours must fail and the rest must still pass. This
proves each scenario's assertion discriminates. A scenario that passes in both modes is a bug in the scenario.

## Manual checks on the owner's PC

These are the "try it" steps that no fake can replace. Each phase document repeats the ones it needs.

| Area | Steps | Expect |
|---|---|---|
| Real Ollama (Phase 0) | Start Ollama, open Settings, press Test connection; open `#/doctor`; found a company from an idea with no context | Test reports native tool calls; every Doctor row ok or explained; founding finishes `done` without a question; the welcome brief arrives |
| GPU fit (Phase 0) | With two agents working, run `ollama ps` | 100% GPU; the Doctor `gpu_fit` row is ok, or warns about spilling into RAM |
| Real Telegram (Phase 0) | Create a bot, put the token and your chat id in Settings, Phone notifications, press Send test message; trigger an approval; press Approve on the phone; send `/status`, `/stop`, `/resume` (verify against the vendor docs before connecting) | The test message arrives within seconds; the approval shows buttons; the message is edited with the outcome; `/stop` shows the red banner on the dashboard |
| Failure within 5 minutes (Phase 0 gate) | Stop Ollama while a company is active with open tasks; block search by switching to a provider with no key and no fallback | One question on the phone with a recommended answer, once, within 5 minutes; answering it from the phone applies |
| Backups (Phase 0) | Wait for the first nightly backup or press the backup button; stop Holdco; unzip a backup over the data folder; start | `data/backups/` has one zip; the restored company is intact |
| Stripe test mode (Phase 1 gate) | Create a test-mode key with the Agent tag (verify against the vendor docs before connecting); add it on the connector card; press Test; ask the CEO for a Starter plan payment link; approve; open the link in a browser; pay with a test card | The card says test mode; health ok; the link opens; within the hour `stripe.revenue_usd_30d` shows the payment with source stripe, and no agent typed it |
| Playwright MCP (Phase 1) | Install Node, enable the playwright connector; run `browser_check site/index.html` from a QA task | The first line is not "Static check only"; `.qa/` has desktop and mobile screenshots; console errors are listed |
| Resend and Buffer (Phase 1) | Verify a sending domain in Resend, add the key, keep email in draft-only until the first approved email looks right; add a Buffer personal token and approve one post (verify against the vendor docs before connecting) | The email arrives from the right sender with an opt-out line; the post appears in Buffer's queue |
| Hosted MCP with OAuth (Phase 1) | Enable `use_mcp` on the Stripe connector or connect the GitHub server (Phase 4); press Sign in | The browser returns to `127.0.0.1` and the card shows connected; no token appears in any transcript |
| Brain and rhythms (Phase 2) | Let a company run for two days with Stripe connected | The morning plan names goals; the evening brief on the phone shows real numbers; the weekly memo cites metrics with sources |
| Bench on a real model (Phase 3 gate) | Settings, Test this model, for the default model and a candidate | Scores appear on `#/bench`; a model that scores low on tool-call validity is visibly worse in a founding run |
| Browser QA gate (Phase 3 gate) | Ask engineering to add a signup form without an action | The task cannot be marked done until the form has a target; the CEO's review shows the gate result |
| Code sandbox (Phase 3) | Install Docker; approve a `run_code` that tries to read a file outside the company folder or reach the network | The read fails and the network is unreachable; without Docker the Doctor shows the warning row |
| The week from the phone (Phase 4 gate) | Run the portfolio for seven days without opening the dashboard except the Doctor page | Every approval, decision, escalation and brief arrived on the phone and could be answered there; the Portfolio page's numbers match Stripe and analytics |
