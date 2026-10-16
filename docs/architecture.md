# Holdco architecture

Holdco is a standard-library-only Python application that runs AI-operated companies on one local GPU. One
process serves the dashboard, runs a scheduler thread, and starts a thread per agent work session. Everything
is stored in one SQLite database plus one folder per company. Today the system is an open loop: agents plan,
write files and queue outbound actions for the owner, but nothing measures the result. The plan in
[plan.md](plan.md) closes the loop: a company brain with measured goals, task contracts verified by tools,
connectors that feed real metrics back, rhythms that turn metrics into the next plan, and the owner as the
board. This document describes the modules, data, threads and trust boundaries of both the system that exists
and the system the phases build.

Status: reference. "Today" sections describe the code on `main` (v0.1.0: 28 tools, 19 presets, 11 tables; the
Phase 0 scaffold on the branch adds `note_progress` and the `audit` table).
"Planned" sections follow the design brief and are built in Phase 0 to Phase 4 (see `phases/`). Phase 0 is in
progress on branch `claude/phase-0-stabilize`.

Related: [operating-model.md](operating-model.md) (brain, goals, contracts, rhythms),
[connectors.md](connectors.md) (MCP, vault, risk tags), [reliability.md](reliability.md) (Doctor, watchdog,
Bench), [safety.md](safety.md) (trust ladder, taint, sandbox), [owner-experience.md](owner-experience.md)
(brief, decisions, phone), [testing.md](testing.md).

## How a company runs today

### The loop

1. `run.py` calls `db.init()`, builds the HTTP server with `server.make_server()`, starts one `Scheduler`
   thread, and serves HTTP on the main thread.
2. `Scheduler.run()` first calls `scheduler.recover()`: `in_progress` tasks go back to `todo`, agents are set
   `idle`, `running` runs become `interrupted`, `running` approvals become `failed`, and any company still in
   status `founding` is founded again. Then it calls `tick()` every `HOLDCO_TICK_SECONDS` (default 8).
3. `tick()` does four things for every company with status `active`:
   - polls the IMAP inbox every 5 minutes in a thread (`emailer.poll_inbox`), when IMAP is configured;
   - when `scheduler_enabled` is on and fewer than `max_concurrent_runs` background runs are active, starts a
     CEO planning cycle if one is due (`cycle_minutes`, default 60, elapsed since `companies.last_cycle_at`),
     if the team ran out of work (no open tasks for 10 minutes), or if `companies.cycle_requested` is set
     (an owner decision sets it);
   - every `HOLDCO_ROUTINE_CHECK_SECONDS` (default 60) creates recurring-duty tasks from each enabled agent's
     preset routines (`presets.R(...)`: `every_hours`, `first_after_hours`, `requires` smtp or imap, a
     `deliverable`), keyed in `tasks.routine` as `<preset>:<title>` so a duty is never queued twice;
   - hands `todo` tasks to agents, ordered by `priority, id` across all companies, assigning by role when the
     task has no agent, and calls `agents.start(agent_id, "task", task_id=...)` until capacity is used.
4. `agents.run()` is one work session of kind `task`, `cycle`, `founding` or `chat`. It takes a GPU slot
   (`llm.background_slot()`, or `llm.chat_slot()` for chat), builds the system prompt and the kind-specific
   user prompt, then loops up to `max_steps` (`max_steps_per_run`, default 14; founding at least 26, cycle at
   least 18, chat at most 8). Each step: trim the transcript to `num_ctx * 2.6` characters, call `llm.chat()`
   with the role's tool specs, publish a `think` event, execute up to 4 tool calls through `tools.execute()`,
   publish a `result` event per call, and append the tool results. A reply with no tool call is nudged up to
   three times, then the run ends `partial`. The loop guard stops a run that repeats an identical call three
   times (the second time gets a warning), except for `check_inbox` and `list_tasks`; a successful
   `write_file`, `delete_file` or `run_code` resets the guard for read-only calls. At step `max_steps - 2` the
   agent is told to save and finish. Each step of a task or cycle run re-checks that the company is `active`
   and the agent `enabled`, otherwise the run ends `stopped`.
5. Tools do the work: files in the company folder, tasks, memory, metrics, the charter, hiring, the three
   quality checks, and the outbound tools, which all go through `actions.gate()`. `finish` ends the session
   with a structured handoff (status, deliverable, summary, evidence, open issues, next step) and, for a task
   finishing `done`, runs the deliverable check: the file must exist, must have been written for this task
   (this session or an earlier attempt, from `file` events carrying the task id) and must be at least 40
   bytes. The first failure returns "Not finished yet: ..."; the second forces status `partial`.
6. `agents._finalize()` writes the outcome: the task status (`done`, `blocked`, `waiting`; `partial`, an
   error or running out of steps sends the task back to `todo`, or to `blocked` from the third attempt), the
   handoff appended to `tasks.result`, the run row with its transcript, `companies.tokens_used`, a `done` or
   `error` event, and the agent back to `idle`. A founding run that ends `done` or `out_of_steps` sets the
   company `active`.
7. The owner closes the loop by hand: the Inbox decides approvals (`actions.decide()`), chat goes to the CEO or
   a named agent (`agents.send_chat()`), and every decision sets `cycle_requested` so the CEO re-plans soon.

### Modules and what each owns

| Module | Owns |
|---|---|
| `run.py` | Process start: database, HTTP server, scheduler thread, browser open, Ctrl+C shutdown |
| `holdco/config.py` | Paths (`DATA_DIR`, `COMPANIES_DIR`, `TRASH_DIR`, `DB_PATH`, `STATIC_DIR`), `HOST`, `PORT`, `PASSWORD`, `DEFAULT_SETTINGS`, `POLICY_ACTIONS`, `DEFAULT_POLICIES`, `LOCKED_POLICIES`, `DEFAULT_COMPANY_SETTINGS`, `WORKSPACE_DIRS` |
| `holdco/db.py` | One SQLite connection behind an `RLock`, WAL mode, `SCHEMA`, `FTS_SCHEMA` (optional FTS5 on memory), `MIGRATIONS` (columns added with `ALTER TABLE`), `q/one/run/scalar/update`, `now()`, settings merge (`get_settings`, `set_settings`), `company_settings`, `effective_policy` |
| `holdco/bus.py` | The event bus: `emit()` stores an `events` row and pushes it to every SSE subscriber; `notify()` pushes a refresh hint without storing it; `subscribe/unsubscribe` queues of 2000 |
| `holdco/scheduler.py` | The 24/7 loop described above, `recover()` after a restart |
| `holdco/agents.py` | Prompts (`system_prompt`, `task_prompt`, `cycle_prompt`, `founding_prompt`, `chat_prompt`), the run loop, `_finalize`, `start` (thread per run), company lifecycle (`create_company`, `found`), `send_chat`, idea generation |
| `holdco/tools.py` | `Ctx` (per-run state), `REGISTRY`, the `@tool` decorator, `execute()` (aliases, argument renames, type coercion, `ERROR:` convention, 7000-character result cap), the 28 tools, `role_tools()` |
| `holdco/actions.py` | `gate()` (policy lookup, dedupe, queue or execute), `decide()` (approve, reject, answer), `execute()` and the executors (SMTP email, posting webhook or X intent link, Netlify deploy, outside HTTP call, `run_code` subprocess, purchase note), `retry()` |
| `holdco/llm.py` | Model client for Ollama (`/api/chat`) and OpenAI-compatible servers (`/v1/chat/completions`): tool schemas, prompt-based JSON tool mode and its parser, `think` effort for gpt-oss, per-model sampling, parse-error retries, model listing, the two semaphores |
| `holdco/roles.py` | `tools_for(agent, kind)` (preset belt plus `finish`; `FOUNDING_TOOLS` for founding; `CHAT_EXTRA` for chat), `add_agent` (`MAX_TEAM` 14, unique role keys) |
| `holdco/presets.py` | 19 role presets (mission, done, workflow, rules, KPIs, tool belt, routines, reasoning, creativity, sources), 6 team templates, `render()` for the prompt |
| `holdco/checks.py` | `audit_page`, `contrast_report`, `review_text`: deterministic checks that need no model |
| `holdco/web.py` | `check_public_url` (blocks private, loopback, link-local, reserved and multicast addresses), `fetch_page`, HTML to text, `search()` over SearXNG, Brave, the optional `ddgs` package, or DuckDuckGo HTML |
| `holdco/emailer.py` | SMTP send, IMAP poll, routing of inbound mail to a task for support (or the CEO) |
| `holdco/workspace.py` | One folder per company under `data/companies/<slug>`, `safe_path` (no escape from the folder), `tree`, `.trash`, move to `data/trash` on delete |
| `holdco/server.py` | `ROUTES`, the `Handler` (auth, JSON API, SSE stream, static files, company previews), `make_server` |
| `holdco/static/app.js`, `index.html`, `style.css` | The dashboard: hash router, views, live updates |

### Data model (every table in `db.py`)

| Table | Purpose | Notable columns |
|---|---|---|
| `settings` | Global settings as JSON values, one row per key; merged over `config.DEFAULT_SETTINGS` and policies | `key`, `value` |
| `companies` | One row per company | `name`, `slug`, `idea`, `mission` (one-liner), `charter` (Markdown, also written to `docs/charter.md`), `status` founding, active, paused, archived; `stage` idea, building, launched, revenue; `settings` JSON (per-company settings and policy overrides); `tokens_used`; `cycle_requested`; `last_cycle_at` |
| `agents` | Team members | `role` (unique key per company, `ceo` is required), `preset`, `name`, `title`, `focus`, `model`, `enabled`, `status` idle, working, error; `current_task_id`, `status_note`, `reasoning`, `routines_on`, `last_run_at` |
| `tasks` | The task board | `title`, `description`, `role`, `agent_id`, `status` todo, in_progress, done, blocked, waiting, cancelled; `priority` 1 to 3, `created_by`, `result` (appended handoffs and notes), `attempts`, `deliverable`, `acceptance`, `routine` |
| `memory` | Shared memory: agent notes, owner notes, decisions recorded by `actions.decide` | `kind`, `content`, `pinned`; `memory_fts` mirrors `content` when FTS5 is available |
| `approvals` | The inbox: outbound requests and owner questions | `action` (send_email, social_post, deploy_site, http_post, run_code, spend_money, question), `summary`, `payload` JSON, `status` pending, running, done, failed, rejected, answered; `result`, `feedback`, `decided_at` |
| `events` | Activity feed, Thinking tab and audit trail of tool calls | `kind` (founding, run, tool, file, task, approval, decision, done, error, info, team, memory, metric, message, charter, email, action, think, result), `message`, `data` JSON; `think` and `result` rows are pruned to the newest 1500 per company |
| `messages` | Owner chat with agents | `sender` owner or agent, `content` |
| `runs` | One row per work session | `kind`, `status` running, done, partial, blocked, waiting, stopped, error, out_of_steps, interrupted; `summary`, `transcript` JSON (up to 2 MB), `tokens`, `steps` |
| `emails` | Sent and received mail | `direction` in or out, `uid`, `message_id`, `status` new, handled, replied, sent |
| `metrics` | KPI time series, typed in by agents with `record_metric` | `name`, `value`, `note` |

Indexes: `ix_events_company`, `ix_tasks_company`, `ix_approvals_status`, `ix_memory_company`,
`ix_messages_company`. Migrations today add `agents.preset`, `agents.reasoning`, `agents.routines_on`,
`tasks.deliverable`, `tasks.acceptance`, `tasks.routine`.

### The approval gate and policies

Every outbound tool calls `actions.gate(company, agent, task_id, action, payload)`. The policy for an action
comes from `db.effective_policy()`: a locked action is always `approve`; otherwise the company's
`settings.policies` override wins, else the global `policies` setting, else `approve`.

| Policy | Behaviour |
|---|---|
| `approve` | The request is queued as a pending `approvals` row (an identical pending payload is not queued twice), an `approval` event is emitted, and the agent is told to continue with other work |
| `auto` | `actions.execute()` runs now; failures are reported back to the agent as `FAILED:` and as an `error` event |
| `off` | The agent is told the owner turned the action off |

Actions today: `send_email`, `social_post`, `deploy_site`, `http_post` (non-GET `http_request`), `run_code`,
`spend_money` (locked, `request_purchase`), and `question` (`ask_owner`, always to the owner).
`actions.decide()` runs the executor on approval (the owner may edit `to`, `subject`, `body`, `content` or
`code` first), records a `decision` memory row, reopens a `waiting` or `blocked` task with the outcome, creates a
p1 "Revise:" task when a rejection carries feedback, and sets `cycle_requested`. `send_email` also enforces the
company's `daily_email_limit` (sent plus pending today).

Planned (Phase 0): the kill switch makes `gate()` queue `auto` actions for approval and makes `decide(approve)`
refuse. Planned (Phase 1): risk tags and the four connector policies, see Trust boundaries below. Planned
(Phase 3): taint and the trust ladder are consulted inside `gate()` before the policy.

### Dashboard structure

Routes in `server.py` (all JSON; non-GET calls need the header `X-Holdco: 1` and a JSON body; Basic auth when
`HOLDCO_PASSWORD` is set):

| Method and route | Purpose |
|---|---|
| `GET /api/state` | Portfolio: companies with stats, pending approvals, running agents, `scheduler_enabled`, model, agents, version |
| `GET`, `PUT /api/settings` | Global settings, `policy_actions`, locked policies, `data_dir` |
| `GET /api/llm/models`, `POST /api/llm/test` | List installed models; ping tool-call test for one model |
| `GET /api/presets` | Preset library, categories, team templates |
| `POST /api/ideas` | Three business ideas from the model |
| `POST /api/companies` | Create a company and start founding |
| `GET`, `PATCH`, `DELETE /api/companies/<id>` | Company detail (agents, stats, latest metrics, email readiness, `has_site`); edit name, mission, stage, charter, status and settings (secrets masked as `••••••••`); delete after typing the name |
| `POST /api/companies/<id>/cycle`, `.../refound` | Start a planning cycle; restart founding |
| `POST /api/companies/<id>/agents`, `PATCH`, `DELETE /api/agents/<id>`, `POST /api/agents/<id>/run` | Hire, edit, remove (never the CEO), run now |
| `GET /api/companies/<id>/runs`, `GET /api/runs/<id>` | Session list; one session with its transcript |
| `GET`, `POST /api/companies/<id>/tasks`, `PATCH /api/tasks/<id>` | Task board |
| `GET`, `POST /api/companies/<id>/messages` | Chat |
| `GET`, `POST /api/companies/<id>/memory`, `PATCH`, `DELETE /api/memory/<id>` | Memory, pinning |
| `GET /api/companies/<id>/metrics` | Metric rows |
| `GET /api/companies/<id>/emails`, `POST .../emails/check`, `POST .../emails/test` | Mail log, IMAP poll now, SMTP test |
| `GET /api/events` | Feed with `company_id`, `agent_id`, `kinds`, `exclude`, `before`, `limit` |
| `GET /api/approvals`, `POST /api/approvals/<id>`, `POST /api/approvals/<id>/retry` | Inbox (`status` pending or decided); decide with `approve`, `reject` or `answer`; retry a failed one |
| `GET /api/companies/<id>/files`, `GET`, `PUT /api/companies/<id>/file` | File tree, read, owner edit |
| `GET /api/stream` | Server-Sent Events |
| `GET /`, `/static/*`, `/preview/<slug>/*`, `/files/<slug>/*` | Dashboard, assets, company site preview and files (served with a sandboxing Content-Security-Policy) |

Views in `app.js`: `render()` reads `location.hash` and mounts one view: `#/` Portfolio, `#/new` NewCompany,
`#/inbox` Inbox, `#/settings` Settings, `#/library` Library, `#/c/<id>/<tab>/<arg>` CompanyView with tabs
`overview`, `chat`, `tasks`, `team`, `mind` (Thinking), `files`, `memory`, `email`, `charter`, `settings`. A
view is a factory returning `{mount(el), live(ev), act(name, t, e), submit(name, form)}`; clicks are wired by
`data-act` and forms by `data-form`. The sidebar lists companies, Inbox with a pending count, Agent library,
Start a company, Settings and the autopilot switch.

SSE bus: `connect()` opens `EventSource('/api/stream')`. Each `bus.emit()` arrives as `{"type": "event", ...}`
and is appended to any mounted feed; each `bus.notify(kind)` arrives as `{"type": kind}` (`approvals`, `tasks`,
`agents`, `messages`, `companies`, `company`, `settings`) and triggers a debounced `refreshState()` plus the
current view's `live(ev)`. The server sends a comment ping every 15 seconds and `retry: 3000` so the browser
reconnects on its own.

## Target architecture

### The closed loop

```
                          OWNER  (phone via Telegram, Inbox, Doctor, Portfolio)
       briefs, board memo, questions  ^           |  goals, approvals, decisions, answers
                                      |           v
  +-------------+   digest    +-------------+  bets   +------------------+
  |    BRAIN    | ----------> |    GOALS    | ------> |  TASK CONTRACTS  |
  | strategy    |             | <= 3 active |         | goal, owner,     |
  | metrics     |             | metric,     |         | checks, deps,    |
  | contacts    |             | target,     |         | budget, due      |
  | decisions   |             | deadline    |         +--------+---------+
  | experiments |             +-------------+                  | scheduler (deps done, budget left)
  | lessons     |                                              v
  +------^------+                                     +--------+---------+
         |                                            |    AGENT RUN     |
         | metrics with a source                      | brain digest,    |
         |                                            | small belt,      |
  +------+------+   polled    +-------------+  gate   | notes, find_tool |
  |   METRICS   | <---------- | CONNECTORS  | <-------+--------+---------+
  | stripe.*    |   hourly    | MCP client, |  policy,         | finish
  | email.*     |             | vault,      |  trust ladder,   v
  | analytics.* |             | health      |  taint, kill  +--+---------------+
  +------+------+             +-------------+  switch       | VERIFICATION GATE |
         |                                                  | browser_check,    |
         v                                                  | sandbox tests,    |
  +-------------+  daily plan, daily brief, weekly review,  | Stripe API,       |
  |   RHYTHMS   |  monthly check <--------------------------+ compliance,       |
  +------+------+                                           | citations         |
         |  brief, board memo, decisions                    +-------------------+
         v
       OWNER
```

Reading the loop: the brain holds the company's state and is read as a digest at the start of every run
(Phase 2). Goals, at most three, drive bets, experiments and task contracts (Phase 2). The scheduler dispatches
a contract only when its dependencies are done and budgets allow (Phase 2). An agent run reaches the outside
world only through the risk gate and connectors (Phase 1), and can only finish `done` with evidence from the
verification gate (Phase 3). Connectors are polled for metrics that land in the brain with their source
(Phase 1). Rhythms read those metrics and write briefs, memos and decisions for the owner (Phase 2), and the
owner's answers, goals and approvals feed back in. The watchdog (Phase 0) breaks the loop into a single
question when it sticks. Portfolio allocation (Phase 4) shifts GPU share toward companies with traction.

### Module map by phase

| Phase | Module | Purpose |
|---|---|---|
| 0 | `routing.py` | Route registry shared by `server.py` and feature modules: `ROUTES`, `route()`, `HTTPError`, `to_int()`, `company_or_404()` |
| 0 | `doctor.py` | Health rows (`run(deep)`, cached in `LAST`), founding `preflight()`, `#/doctor` page data |
| 0 | `watchdog.py` | `tick()` patterns (task_failed_twice, repeated_error, task_bouncing, connector_down, stalled, founding_partial), escalation questions with options, `resolve()`, working notes (`note_progress`, auto notes, retry guidance) |
| 0 | `notify.py` | Telegram and ntfy sending, the long-polling thread, callback handling, `/status`, `/stop`, `/resume` |
| 0 | `backup.py` | `run_backup()` zips the database (via `Connection.backup`), the companies folder and the vault file; keeps `backup_keep` |
| 1 | `vault.py` | Secret store: DPAPI, Secret Service via `secret-tool`, or an HMAC-authenticated file; `put/get/delete/names/backend/redact` |
| 1 | `mcp.py` | MCP `Client`: stdio and Streamable HTTP, both protocol generations, `list_tools`, `call_tool`, `AuthRequired` |
| 1 | `oauth.py` | Authorization code with PKCE, discovery (RFC 9728, RFC 8414), dynamic registration (RFC 7591), token storage per issuer |
| 1 | `connectors.py` | Connector `DEFINITIONS`, instances, health checks, `poll_metrics()`, `find_tool`, `call()`, the `connector_call` executor |
| 1 | `conn_stripe.py`, `conn_resend.py`, `conn_playwright.py`, `conn_buffer.py` | The first connectors; Brave stays built in to `web.py` |
| 1 | `browser.py` | `browser_check` through Playwright MCP with a labelled static fallback |
| 2 | `brain.py` | Brain tables, `digest()`, brain tools, proposal review routing |
| 2 | `rhythms.py` | Daily plan, daily brief, weekly review and board memo, monthly strategy check; `briefs` rows |
| 3 | `bench/` (`scenarios.py`, `runner.py`, `mocks.py`, `__main__.py`) | Holdco Bench |
| 3 | `verify.py` | The verification gate run by `finish(status=done)` per kind of work |
| 3 | `sandbox.py` | `run_code` in docker or podman with limits; unsandboxed fallback with a warning |
| 3 | `trust.py` | The trust ladder per company and action |
| 3 | `embeddings.py` | Vector memory search through the embedding model, FTS fallback |
| 4 | `mcp_server.py` | Holdco as an MCP server at `/mcp` and over stdio (`python -m holdco.mcp_server`) |
| 4 | `portfolio.py` | Capital allocation score, scheduler share, monthly portfolio review |
| 4 | `conn_porkbun.py`, `conn_cloudflare.py`, `conn_github.py`, `conn_plausible.py` (and umami), `conn_forms.py`, `conn_n8n.py` | The remaining connectors; comfyui, ads and accounting as "not yet available" definitions |

Edits to existing modules by phase: Phase 0 touches `web.py` (fallback chain and `PROVIDER_STATUS`),
`agents.py` (kill switch, notes, retry guidance), `tools.py`, `actions.py` (escalation answers, audit rows),
`scheduler.py` (watchdog tick, doctor refresh, backup, Telegram poller), `server.py`, `config.py`, `db.py`,
`llm.py` (`loaded_models`, `tool_ping`), `emailer.py` (`check_smtp`, `check_imap`), `bus.py` (error hook to
notify), `roles.py` (`TASK_EXTRA`, committed), `app.js`, `style.css`. Phase 1 adds the `source` column, risk tags on every built-in tool and vault markers in
settings readers. Phase 2 extends `create_task`, the scheduler (dependencies, budgets), `workspace.py` and
`tools.py` (leases) and `actions.py` (decisions). Phase 3 edits `llm.py` (structured outputs,
`pick_model(job)`), `tools.py` (argument repair, `Ctx.tainted`), `actions.py` (taint and trust in `gate()`) and
`scheduler.py` (dispatch priorities, quiet hours).

### New tables and columns by phase

| Phase | Table or column | Purpose |
|---|---|---|
| 0 | `audit(id, created_at, company_id, action, proposed_by, approved_by, payload, result, approval_id)` | Append-only log of every executed outbound action |
| 0 | `tasks.notes`, `tasks.reassigned` | Working notes that survive retries; reassignment counter for the bouncing pattern |
| 1 | `connectors(id, company_id, key, enabled, config, status, detail, last_check, created_at)` | Connector instances; `company_id` NULL for global; secrets in the vault as `connector.<id>.<field>` |
| 1 | `metrics.source` | `manual` for existing rows; a connector key for polled metrics named `<connector>.<metric>` |
| 1 | `emails.in_reply_to` | The In-Reply-To header of inbound mail, so replies to outbound messages can be counted |
| 2 | `strategy`, `goals`, `contacts`, `decisions`, `experiments`, `lessons`, `brain_changes` | The company brain |
| 2 | `tasks.goal_id`, `expected`, `inputs`, `depends_on`, due date (`due_days` at creation), `budget_steps`, `budget_tokens`, `checks` | Task contracts |
| 2 | `leases(company_id, path, task_id, agent_id, run_id, expires_at)` | File leases, 30-minute expiry |
| 2 | `briefs(id, company_id, kind, body, data, created_at)` | Daily, weekly and monthly briefs; `company_id` NULL for the portfolio; Phase 4 adds kind `portfolio` |
| 2 | `tasks.tokens_used`, `approvals.decided_by` | Token budget accounting per task; who answered a decision (`owner`, `telegram`, `deadline`) |
| 3 | `bench_runs` | Bench history |
| 3 | `trust(company_id, action, level, streak, cap_per_day, updated_at)` | The trust ladder |
| 3 | `memory_vectors(memory_id, model, vector)` | Embeddings for memory search |
| 3 | `runs.max_prompt_tokens` | Context use per run for the Settings warning |
| 3 | `tasks.verification`, `tasks.followup_at` | The gate's results as JSON; when an owner decision reopened the task (dispatch class 1) |
| 3 | `suppression(email, reason, company_id, created_at)` | The global opt-out list every `send_email` is checked against |

New tables go in `db.SCHEMA` with `CREATE TABLE IF NOT EXISTS`; new columns go in `db.MIGRATIONS`.

### Extension points

| Extension point | How it works |
|---|---|
| `holdco/routing.py` | Holds `ROUTES`, `route()`, `HTTPError`, `to_int()` and `company_or_404()`. `server.py` imports them (keeping `_int` and `_company_or_404` as aliases) and its `Handler` matches `ROUTES` in order |
| `@route(method, pattern)` | A feature module registers JSON routes at import time: `fn(h, m, body)` returns a JSON-serialisable dict; `h.query` holds the parsed query string; raise `HTTPError(status, message)` for errors |
| Feature imports in `server.py`'s import line | `backup`, `doctor`, `notify` and `watchdog` are imported in the module's top-level `from . import ...` line (marked `# noqa: F401`), so their routes exist before the first request; later phases add their modules there |
| `@tools.tool(name, description, props, required)` | A feature module registers agent tools; Phase 1 adds `risk=` to the decorator. The tool is only reachable by roles whose preset belt lists it, or through `find_tool` |
| Feature imports at the bottom of `tools.py` | `from . import watchdog  # noqa` and later modules, placed after `tool`, `S`, `I`, `Ctx` and `ToolError` are defined so the decorator exists when the module runs |
| `connectors.DEFINITIONS` (Phase 1) | A connector is a dict: label, category, kind (`api`, `mcp-stdio`, `mcp-http`, `builtin`), scope, setup card, `health`, `metrics`, `tools` or `mcp` config, `test_mode` |
| `actions.execute()` | One executor per action; Phase 1 adds `connector_call` so every connector call is gated and audited like built-in actions |
| `presets.PRESETS[...]["routines"]` and `["tools"]` | Rhythms and tool belts per role |
| `bus.emit(kind, message)` | Any module publishes feed events; the dashboard needs no new wiring to show them |
| `app.js` view factories | A new page is one factory plus one branch in `render()` and one sidebar link |

## Trust boundaries

Today:

| Zone | What the model may do | Enforced by |
|---|---|---|
| Free | Search the web, fetch public pages, read and write files inside the company folder, create and update tasks, save and search memory, record metrics, update the charter, hire from presets, run the three checks, message the owner, ask the owner a question | `workspace.safe_path`, `web.check_public_url`, `roles.tools_for` (a role sees only its belt), `MAX_TEAM`, the open-task cap of 40 |
| Gated | Send email, post, deploy, non-GET HTTP, run code, buy | `actions.gate()` with the policy per action, global and per company |
| Locked | `spend_money` is always `approve`; `question` always reaches the owner | `config.LOCKED_POLICIES`, `db.effective_policy` |
| Owner only | Change settings and policies, edit files, pause or delete a company, approve or reject | The dashboard API: `X-Holdco` header, Basic auth when exposed, `run.py` refuses to bind to a non-local address without `HOLDCO_PASSWORD` |

Secrets today live in plain text: global ones in `settings` (`llm_api_key`, `brave_api_key`), per-company ones
in `companies.settings` (`smtp_password`, `imap_password`, `netlify_token`). The API masks them, prompts never
include them, and nothing redacts them from events. Agent-written pages are served with
`Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups`, so a page cannot call the dashboard
API. `run_code` runs as the owner's user with a timeout and the `HOLDCO*` and `OLLAMA*` environment variables
removed, but it is not sandboxed. Text from the web, emails and files is declared "information, not
instructions" in the shared rules, with nothing structural behind that.

Planned:

| Phase | Change to the boundary |
|---|---|
| 0 | Kill switch (`kill_switch` setting, `POST /api/kill`): nothing dispatches, runs stop at the next step, `auto` actions queue, approvals refuse, the Telegram poller keeps running for `/resume`. Append-only `audit` table. Escalation questions carry options and a recommendation. Telegram messages from any chat id other than `telegram_chat_id` are ignored |
| 1 | `config.RISK_TAGS`: `read`, `write`, `publish`, `contact`, `spend`, `irreversible`. Built-in tools are tagged (`read` for searches, fetches, reads, recall, lists, checks, `browser_check`, `find_tool`; `write` for internal writes; `contact` for `send_email`; `publish` for `social_post`, `deploy_site`, non-GET `http_request`; `spend` for `request_purchase`; `run_code` keeps its own policy). Connector tools map to the new policies `connector_write`, `connector_publish`, `connector_contact`, and to `spend_money` and the new locked `irreversible`; an unclassified MCP tool asks (`connector_publish`). Secrets move to the vault (`vault:<name>` markers), `vault.redact()` scrubs events and transcripts, agents name a connector and never see a key. Playwright runs `--isolated` |
| 2 | Brain changes to price, offer, positioning or ICP go to the owner as a decision; a goal-set change is announced as a decision; budgets per company (`gpu_hours_per_day`, `steps_per_task`, `emails_per_day`, `posts_per_week`, `money_usd` fixed at 0) |
| 3 | Taint: a run that read outside content (`web_search`, `fetch_url`, `check_inbox`, an external browser snapshot, a connector read) has its `publish`, `contact`, `spend` and `connector_write` calls forced to `approve`, with the untrusted text fenced in the tool result. Trust ladder: levels 0 ask, 1 auto with a daily cap, 2 auto, per company and action; promotion suggested at a streak of 15 clean approvals, demotion on a rejection; never promoted: `spend_money`, `irreversible`, emails that read like legal commitments, the first message to a new contact. `run_code` in a container with no network by default |
| 4 | Holdco's own MCP server is password-protected when `HOLDCO_PASSWORD` is set and local-only otherwise. Porkbun defaults to sandbox keys and dry runs; domain purchases are `spend` plus `irreversible` |

Where secrets live after Phase 1: the vault backend (`dpapi`, `secret-service` or `file`), with `data/vault.json`
and, for the file backend without `HOLDCO_VAULT_PASSPHRASE`, `data/vault.key` (mode 0600). OAuth tokens are
stored under `oauth.<connector id>` and sent only to their issuer's resource. The backup includes the vault file,
and a secret records its backend so a restore on another machine fails loudly.

## Deployment and process model

One process, started by `run.py` (or `start_windows.bat`, `start_linux.sh`, the `autostart.py` helpers for a
login item or a systemd user service). Data lives under `data/` (or `HOLDCO_DATA`): `holdco.db` (WAL, so
`holdco.db-wal` and `holdco.db-shm` exist while running), `companies/<slug>/` with `docs`, `research`, `site`,
`code`, `marketing`, `sales`, `support`, `ops`, plus `.runs` and `.trash`, and `trash/` for deleted companies.
Planned additions: `backups/` (Phase 0), `vault.json` and `vault.key` (Phase 1), `.qa/` screenshots inside a
company folder (Phase 1).

Threads today:

| Thread | Started by | Does |
|---|---|---|
| Main | `run.py` | `ThreadingHTTPServer.serve_forever()`; each request gets a daemon thread; an SSE client holds its thread for the life of the connection |
| `scheduler` | `run.py` | `tick()` every 8 s; routine creation every 60 s; dispatch |
| `agent-<id>-<kind>` | `agents.start()` from the scheduler, the API or chat | One work session; at most one per agent (`_running`) |
| IMAP poll | `Scheduler._check_inbox` | `emailer.poll_inbox` per company, every 5 min |
| Chat wait | `agents.send_chat` when the agent is busy | Polls every 2 s for up to 20 min, then runs the chat |
| Browser timer | `run.py` | Opens the dashboard once |

GPU slots: `llm.background_slot()` is a `BoundedSemaphore(max_concurrent_runs)` rebuilt when the setting
changes; a task, cycle or founding run holds it for the whole session. `llm.chat_slot()` is a separate
`BoundedSemaphore(1)`, so owner chat never waits behind background work (Ollama itself must allow
`OLLAMA_NUM_PARALLEL` requests). The scheduler also refuses to dispatch when `agents.running_count()` (chat
excluded) reaches capacity. The database is one connection behind an `RLock`; every helper takes it.

Planned threads: the Telegram long-polling thread (Phase 0; started by the scheduler when
`telegram_bot_token` and `telegram_chat_id` are set; `getUpdates` with a 25 s timeout; restarts with backoff),
the backup thread (Phase 0; started by the scheduler when the last backup is older than 24 h), hourly doctor,
connector health and `poll_metrics()` work inside the scheduler (Phases 0 and 1), one stdout reader thread per
stdio MCP `Client` and the Playwright MCP subprocess (Phase 1), the Bench runner thread behind
`POST /api/bench/run` (Phase 3). Phase 3 changes dispatch order to chat, then follow-ups to fresh owner
decisions, then tasks due within 24 h, then p1, normal, routines, with low-value research held for
`quiet_hours`. Phase 3 also moves the background slot from the whole session to each model call, so agents yield
at step boundaries and a chat reply or a follow-up gets the GPU at the next step of a long run. Phase 4 lets the
allocation score decide which company gets a free slot first, with a floor of one run per cycle per company.

What runs when Holdco is closed: nothing. There is no cloud component, no webhook receiver and no open port;
inbound data is polled, and Telegram uses long polling. A closed Holdco means no agents, no polling, no
notifications and no backups, which is why the autostart helpers and "do not sleep" guidance exist. On the next
start `scheduler.recover()` resumes interrupted work, and from Phase 0 the working notes let a retried task
continue where the last attempt stopped.
