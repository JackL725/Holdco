# Phase 0: Stabilize

Phase 0 makes Holdco tell the owner when something is wrong and keeps a failed run from starting over blind. It
adds a Doctor page with a founding preflight, a search fallback chain, a watchdog that turns stuck patterns into
one question with a recommended answer, working notes that survive a failed attempt, Telegram and ntfy
notifications with approval buttons, a kill switch, nightly backups, an append-only audit log, and the settings
that drive them. Everything is standard library, runs on Windows and Linux, and is proven by fakes in the test
suite. The "why" is in [../plan.md](../plan.md) (Phase 0 and "Making a local model reliable") and in
[../reliability.md](../reliability.md); this document is the "how".

Status: planned; scaffold committed, behaviour not built. Branch `claude/phase-0-stabilize`, PR to `main`.
Commit `3043085` ("Phase 0 scaffold") added `holdco/routing.py`, the settings, the `audit` table, the
`tasks.notes` and `tasks.reassigned` columns, the kill switch, the founding preflight hook, `GET /api/audit`,
`POST /api/kill`, and skeleton modules `doctor.py`, `watchdog.py`, `notify.py`, `backup.py` whose functions raise
`NotImplementedError` (every hook tolerates that, so the suite stays green). This document names those hooks
exactly; the implementer fills them in.

Related: [../architecture.md](../architecture.md), [../operating-model.md](../operating-model.md),
[../safety.md](../safety.md), [../owner-experience.md](../owner-experience.md), [../testing.md](../testing.md),
[phase-1-connect.md](phase-1-connect.md).

## Scope

From the plan's Phase 0 list, plus the two items the brief adds (audit log, settings):

- Doctor page and founding preflight (`doctor.py`).
- Search fallback chain (`web.py`).
- Watchdog with escalation (`watchdog.py`).
- Working notes and retry-differently (`tasks.notes`, `note_progress`, `agents.py`, `watchdog.py`).
- Telegram notifications and approvals, ntfy as notify-only (`notify.py`).
- Kill switch (setting, route, scheduler, agent loop, gate).
- Nightly backups (`backup.py`).
- Audit log (table `audit`, route, Inbox panel).
- Settings for all of the above (`config.DEFAULT_SETTINGS`).

Gate: a new company founds with no help from the owner, and any failure reaches the owner's phone within 5
minutes as one clear question.

## What the first real run showed

The plan's table says it in one line: "search was blocked, the CEO gave up with a 'partial' note, and nothing
told you why." Mapped onto the code as it is today:

| Symptom | Where it happens today | Why nobody was told |
|---|---|---|
| Search blocked | `web.search()` tries the `ddgs` package, then `web._ddg_html()`. DuckDuckGo answered with a bot-challenge page. `_ddg_html` swallows errors (`continue`) and returns `[]`, so the `web_search` tool said "No results. Try different words." | No event is emitted, no provider is marked bad, and the only other providers (Brave, SearXNG) are used only when selected in Settings. The agent retried new words against the same blocked provider. |
| Founding partial | `agents._finalize`: a founding run that ends `done` or `out_of_steps` sets the company `active` and emits "is up and running". A `finish(status='partial')` call counts as `done` here because `ctx.finished` is true. | The company went live on a thin charter. The only signal was the word "partial" inside the run summary. The error path (`status == "error"`) shows a red banner on the Overview tab, nowhere else. |
| Parse error | Ollama returns HTTP 500 "error parsing tool call" when the model writes malformed JSON inside a long `write_file` argument. Commit `695ad27` added `llm._call_with_parse_retry` (`PARSE_RETRIES = 2`). A third failure still ends the run with status `error`. | The task goes back to `todo` and the next attempt restarts from scratch with only the last 1,500 characters of `tasks.result` and the line "This is attempt N". Nothing says what went wrong or what to avoid. |
| Loop guard | `agents.run`: an identical call twice gets a nudge; three times forces a `partial` finish ("stopped after repeating the same action"). Commit `695ad27` lets an agent re-check a file after changing it. | A forced partial keeps nothing but a one-line note. Attempts bump at run start; after 3 attempts the task is `blocked`, visible only on the Tasks tab and in the CEO's next cycle prompt. |

The common thread: failures are recorded in transcripts and task notes, not turned into a question for the
owner, and a retry does not know why the previous attempt failed.

## Design

Conventions that apply to every item: SQLite through `db.q/one/run/scalar/update`, new tables in `db.SCHEMA`,
new columns in `db.MIGRATIONS`, UTC strings from `db.now()`, events through `bus.emit(company_id, kind, message,
agent_id, data)`, tool errors start with `ERROR:`, routes registered with `@routing.route(method, pattern)` and
handlers shaped `fn(h, m, body)` returning a JSON-able dict. Feature modules import `routing`, never `server`.

### Scaffold hooks (committed in `3043085`)

| Hook | Where | What it calls |
|---|---|---|
| Route registry | `holdco/routing.py` | `ROUTES`, `route()`, `HTTPError`, `to_int()`, `company_or_404()`; `server.py` keeps the old names `_int` and `_company_or_404` as aliases and imports `backup, doctor, notify, watchdog` so their routes register |
| Scheduler housekeeping | `scheduler.Scheduler._housekeeping(s)`, every tick, before the kill-switch and autopilot checks | `watchdog.tick()`, `doctor.refresh_if_stale()`, `backup.maybe_run()`, `notify.ensure_poller()`; each wrapped so `NotImplementedError` is ignored and other exceptions are printed |
| Failed attempt | `agents._finalize`, task branch, when the run was not clean (`ctx.finished and ctx.finish_status in ("done", "waiting", "blocked")` is false) | `watchdog.add_note(task_id, agents._auto_note(...), "auto")` then `watchdog.task_failed(task, note, run_status)` |
| Founding outcome | `agents._finalize`, founding branch | `watchdog.founding_ended(company, run_status, summary)` when the run ended `out_of_steps` or finished with a status other than `done` |
| Retry guidance | `agents.task_prompt` when `attempts > 1` | `watchdog.retry_guidance(task)`; falls back to the old "(This is attempt N ...)" line |
| Working notes tool | `watchdog.note_progress`, registered with `@tools.tool`; `tools.py` imports `watchdog` at its bottom; `roles.TASK_EXTRA = ["note_progress"]` is added to every role in task runs | `watchdog.add_note(task_id, notes, agent name)` |
| New request | `actions.gate`, after `bus.notify("approvals")`, for every queued row including questions | `notify.approval_requested(row)` |
| Question answered | `actions.decide`, when the row is a `question` whose payload has `pattern` or `options` | `watchdog.resolve(row, answer)` |
| Outbound action ran | `actions.gate` (auto) and `actions.decide` (approve) | `actions.audit(company_id, action, proposed_by, approved_by, payload, result, approval_id=None)` |
| Kill switch | `actions.gate` (auto becomes approve), `actions.decide` (approve raises), `agents.run` (step boundary, all kinds except chat), `scheduler.tick` (returns after housekeeping) | setting `kill_switch` |
| Preflight | `server.api_company_create`, unless `body.force` | `doctor.preflight()`; a hard failure raises `server.PreflightError` (409 with `checks`) |
| State | `server.api_state` | `kill_switch` and `doctor.summary()` |

### 1. Doctor and founding preflight (`doctor.py`)

Behaviour. `doctor.run(deep=False) -> list[dict]` runs every check, stores the rows in `doctor.LAST` under
`doctor._lock`, and returns them. Each row is `{key, label, status, detail, fix, checked_at}` with `status` in
`ok | warn | bad` and `fix` either `None` or `{"kind": "link" | "action" | "text", "label": str, "href": str}`
(`href` holds the hash route for `link`, the action name for `action`, and is absent for `text`).
`doctor.summary(rows=None)` (exists) returns `{ok, warn, bad, checked_at}`. `doctor.refresh_if_stale(max_age_minutes=60)`
runs the checks in a daemon thread when `LAST` is empty or older than the limit and no refresh is already running
(`doctor._refreshing` flag). `deep=True` additionally runs the model tool-call ping and one live search through
the chain. Checks run in parallel threads with a 15 s cap each so one slow connector does not stall the page;
a check that exceeds the cap reports `bad` with detail "timed out".

| Row key | Label | How it is checked | ok / warn / bad | Fix |
|---|---|---|---|---|
| `model_server` | Model server | `llm.list_models(settings)` (Ollama `GET /api/tags`, or `/v1/models` for the OpenAI backend) | reachable / n.a. / unreachable or HTTP error | link `#/settings`, label "Model settings" |
| `model` | Default model | `default_model` in the list above | installed / n.a. / missing | text `ollama pull <model>` |
| `gpu_fit` | GPU memory fit | Ollama `GET /api/ps` via new `llm.loaded_models(settings)`; a loaded model with `size_vram < size` is spilling into RAM. `nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader` adds detail when the binary exists. Unknown (OpenAI backend, nothing loaded) is ok with a note. | fits / spilling / n.a. | text pointing at the README section "Getting the most out of an RTX 5090" |
| `search` | Web search | From `web.PROVIDER_STATUS` (no network); deep = one live `web.search("holdco doctor check", 3, settings)` | at least one provider ok or untested / some providers cooling down / every provider failed in its last try | link `#/settings` |
| `conn:<cid>:smtp` | Email sending (company name) | `emailer.check_smtp(cs)`: connect, STARTTLS or SSL, login, quit | login ok / n.a. / failed | link `#/c/<cid>/settings` |
| `conn:<cid>:imap` | Email receiving (company name) | `emailer.check_imap(cs)`: connect, login, logout | same | same |
| `conn:<cid>:netlify` | Website deploys (company name) | `GET https://api.netlify.com/api/v1/user` with the token | 200 / n.a. / 401 or network error | same |
| `conn:<cid>:webhook` | Posting webhook (company name) | URL syntax only (`urllib.parse.urlparse`: scheme http(s), host present); nothing is posted | valid / n.a. / malformed | same |
| `telegram` | Telegram bot | `notify.check_telegram(settings)` (the Bot API's `getMe`) when a token is set | ok / token set but no chat id / request failed | link `#/settings` |
| `ntfy` | ntfy | topic set and `ntfy_server` parses as a URL; nothing is sent | set / n.a. / malformed | link `#/settings` |
| `disk` | Disk space | `shutil.disk_usage(config.DATA_DIR)` | >= 5 GB / < 5 GB / < 1 GB | text |
| `backup` | Last backup | `backup.last_backup()` age | < 26 h / < 72 h / older or none | action `backup`, label "Back up now" |
| `errors_24h` | Errors in the last 24 h | `SELECT message, COUNT(*) FROM events WHERE kind='error' AND created_at >= ? GROUP BY message ORDER BY 2 DESC` | 0 / 1-9 / >= 10; detail = top 3 messages with counts | link `#/`, label "Open the live feed" |

Connector rows exist only for companies that have the connector configured (`emailer.smtp_configured`,
`emailer.imap_configured`, `netlify_token`, `webhook_url`). Archived companies are skipped. Phase 1 adds
`connector:<instance id>` rows for the connectors table.

`conn:*` login rows are rechecked hourly; a row that has been `ok` for 24 h (`doctor._ok_since[key]`, set when
the row first turns ok and cleared when it stops being ok) is rechecked every 6 h, so consumer mail providers
such as Gmail are not hit with hourly logins. `refresh_if_stale` reuses the cached row in between; a manual
`Run checks` (`run(force=True)`, which `POST /api/doctor/run` passes) always rechecks it.

Founding preflight. `doctor.preflight() -> dict` returns, as the scaffold's docstring says,
`{"ok": bool, "checks": [rows], "soft": [rows]}`: `checks` holds the hard rows `model_server` and `model` (run
fresh), `soft` holds the `search` row (the cached one when `LAST` is younger than 1 h, else one quick live
search), and `ok` is true when no hard row is `bad`. `server.api_company_create` (committed) raises
`PreflightError(409)` when `ok` is false and `body.force` is not true; the JSON body is
`{"error": "Fix this before founding: Model server: <detail>; Default model: <detail>", "checks": [hard rows]}`.
A soft failure founds anyway: `agents.founding_prompt` calls `doctor.search_down()` (true when every provider's
last try failed or is cooling down) and replaces step 1 with: "Web search is down right now. Skip the research
step: write the charter from the idea and what you already know, mark the research gaps in research/market.md,
and ask the owner with report_to_owner to tell you about the market. Do not call web_search more than once."

Data. No tables. `doctor.LAST`, `doctor._lock`, `doctor._refreshing`, `doctor._ok_since` in memory.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/doctor` (committed route; to do: call `run()` when `LAST` is empty so the page never shows nothing) | none | `{"checks": LAST, "summary": {ok, warn, bad, checked_at}}` |
| POST | `/api/doctor/run` (committed) | `{"deep": bool}` | same shape, fresh rows |
| POST | `/api/companies` (committed) | existing body plus `"force": bool` | existing response, or `409 {"error", "checks"}` |

`/api/state` (committed) carries `"doctor": {ok, warn, bad, checked_at}` and `"kill_switch"`.

UI. New page `#/doctor` (view factory `Doctor()` in `app.js`, added to `render()`), listed in the main sidebar
nav as "Doctor" after Agent library with a dot: `.dot.active` (green) when `bad == 0 && warn == 0`,
`.dot.paused` (amber) when `warn > 0`, new `.dot.bad` (red) when `bad > 0`, drawn from `S.st.doctor`. The page is
one `.panel` with a header row (`Run checks`, `Run deep checks`, "Checked 3 min ago") and one row per check: a
`.badge good|warn|bad`, the label, the detail, and the fix (an `<a>` for `link`, an `.btn sm` with
`data-act="fix" data-fix="backup"` for `action`, plain text for `text`). `fix=backup` posts `/api/backup` then
`/api/doctor/run`. The New company page handles a 409 from `POST /api/companies` by rendering the `checks` under
the form in a `.banner bad` ("Holdco found a problem that would stop founding") with a `Found anyway` button
(`data-act="found-anyway"`) that resubmits the same form with `force: true`.

Agent-facing changes. Only the founding prompt text above. Agents never see the Doctor rows.

Code touch points. Fill `doctor.run` (with `force` and the `_ok_since` back-off), `doctor.preflight`,
`doctor.refresh_if_stale`; `doctor.api_doctor` calls `run()` when `LAST` is empty; add `doctor.search_down()`
and `doctor.row_status(key)` (used by the watchdog). `llm.loaded_models(settings)` (wraps
`_request(base + "/api/ps", None, headers, 10)`, returns `[{"name", "size", "size_vram"}]`, Ollama only).
`llm.tool_ping(model, settings) -> (ok, message)` holds the ping now inlined in `server.api_llm_test`, which
becomes a one-liner; `doctor.run(deep=True)` calls the same function. `emailer.check_smtp(cs)` and
`emailer.check_imap(cs)` (new, raise `EmailError`). `agents.founding_prompt`: the search-down branch.
`app.js`: `Doctor()`, sidebar entry, 409 handling in `NewCompany.submit` and `actPick`.

### 2. Search fallback chain (`web.py`)

Behaviour. Four providers, each a function `(query, n, settings) -> list[dict]` that raises `WebError` on any
failure, including a bot challenge:

| Provider name | Needs | Blocked when |
|---|---|---|
| `brave` | `brave_api_key` | HTTP 401/403/429 or network error |
| `searxng` | `searxng_url` | network error, non-JSON, HTTP >= 400 |
| `ddgs` | the optional `ddgs` package | import fails ("not installed", not a block), empty result, exception |
| `ddg_html` | nothing | HTTP 202/403/429, or a page whose text contains "anomaly" or "challenge", or no parsable results on both the html and the lite endpoint |

Order: the configured `search_provider` first (`duckduckgo` means `ddgs` then `ddg_html`), then the rest in the
fixed order `brave, searxng, ddgs, ddg_html`. A provider whose requirement is missing is reported as "no key" or
"no url" and skipped without a network call. `web.search(query, n, settings)` walks the list, skips providers
whose `cooldown_until` is in the future, returns the first non-empty result set, and records the provider that
answered in `web.LAST_PROVIDER`. A provider that raises, returns nothing, or is blocked is recorded in
`web.PROVIDER_STATUS[name] = {"ok": False, "last_ok": iso|None, "last_error": iso, "error": str,
"cooldown_until": epoch + 600}`; a success records `{"ok": True, "last_ok": iso, "error": "", "cooldown_until": 0}`
(`last_error` is kept). When every provider fails or is cooling down, `search` raises
`WebError("All search providers failed: duckduckgo: blocked (HTTP 202 challenge); brave: no key; searxng: no url")`
listing each provider in chain order with its reason, and increments `web.SEARCH_FAIL_STREAK` (reset to 0 by any
success); the watchdog reads that counter.

The `web_search` tool catches that error and returns, for the agent: "ERROR: web search is unavailable right now
(<reason list>). Do not retry it this session. Continue with sources you already know (fetch_url works), note the
gap with note_progress, and say in your handoff that research was skipped." It emits
`bus.emit(cid, "error", "Web search is down: <reasons>")` at most once per 30 minutes across all companies
(`web.LAST_SEARCH_ALERT` epoch), so the owner is told once and `notify` (item 5) forwards it once.

Data. `web.PROVIDER_STATUS`, `web.LAST_PROVIDER`, `web.SEARCH_FAIL_STREAK`, `web.LAST_SEARCH_ALERT` in memory. No
settings change: `search_provider` keeps its values `duckduckgo | searxng | brave`. For tests, the environment
variable `HOLDCO_SEARCH_PROVIDERS` (comma-separated provider names) restricts `_order()` to those providers so a
test can make the chain fail without touching the public internet.

API and UI. No new routes. The Doctor `search` row's detail lists each provider's state ("brave: ok 2 min ago;
duckduckgo: blocked (HTTP 202 challenge), retry in 7 min; searxng: no url").

Code touch points. `web.search` is rewritten around `PROVIDERS = ("brave", "searxng", "ddgs", "ddg_html")`,
`_order(settings)`, `_brave`, `_searxng`, `_ddgs`, `_ddg_html` (the existing function, changed to raise
`WebError("blocked (HTTP 202 challenge)")` instead of returning `[]` on a challenge and to keep the last
exception), and `_record(name, ok, error)`. `tools.web_search` replaces its generic "Search failed" line with the
message above. `TestWorkspaceAndWeb.test_ddg_parser` stays; new tests in `test_phase0.py` monkeypatch the
provider functions.

### 3. Watchdog (`watchdog.py`)

Behaviour. `watchdog.tick()` runs on every scheduler tick from `_housekeeping`, with a handful of SQL queries
and no model calls. Each pattern has a dedupe key (stored in the escalation payload as `pattern`), a question,
options, and a recommended option. Two patterns are raised directly from `agents._finalize` rather than found by
polling. `watchdog.PATTERNS` (committed) lists the six names.

| Pattern key | Trigger | Question (text) | Options | Recommended |
|---|---|---|---|---|
| `task_failed_twice:<task_id>` | `watchdog.task_failed(task, reason, run_status)` finds `attempts >= max_task_attempts` (default 2) | "Task #12 'Build landing page' failed twice (last time: ran out of steps). What should we do?" | `Skip this task`, `Retry once more with my note`, `Pause the company` | `Skip this task`, or `Retry once more with my note` when priority is 1 |
| `repeated_error:<cid>:<hash>` | >= 3 events of kind `error` in the last hour for one company with the same `normalise_error(message)` (lowercase, text up to and including "hit an error: " dropped, digit runs replaced by `#`, whitespace collapsed, first 120 chars; hash = first 8 hex of sha1) | "The same error happened 3 times in the last hour at PawPlan: <message>. What should we do?" | `Pause the company`, `Keep going` | `Pause the company` |
| `task_bouncing:<task_id>` | `tasks.reassigned >= 3` | "Task #12 has been handed between agents 3 times without finishing. What should we do?" | `Skip this task`, `Retry once more with my note`, `Pause the company` | `Retry once more with my note` |
| `connector_down:<row key>` | a Doctor row with key `search`, `conn:*` or (Phase 1) `connector:*` is `bad` on 2 consecutive checks; once per row per day | "Email sending for PawPlan has failed twice in a row (<detail>). Agents are told not to plan work that needs it. What should we do?" | `Keep going without it`, `Pause the company` | `Keep going without it` |
| `stalled:<cid>` | company `active`, `scheduler_enabled` true, open tasks exist, no row in `runs` started in 2 h, and (`model_server` row is `bad` or every run finished in the last 2 h has status `error`) | "Agents at PawPlan are stuck: <reason>. What should we do?" | `Pause the company`, `Keep going` | `Pause the company` |
| `founding_partial:<cid>` | `watchdog.founding_ended(company, run_status, summary)` with `run_status` in `partial`, `out_of_steps` | "Founding PawPlan stopped before it finished (<run_status>). A charter exists. / No charter was written. What should we do?" | `Continue anyway`, `Retry founding`, `Pause the company` | `Continue anyway` when `companies.charter` is non-empty, else `Retry founding` |

For `connector_down` the "two consecutive checks" are two consecutive observations of the row as `bad`:
`doctor.row_status(key)` for `conn:*` rows (hourly, or every 6 h once the row has been ok for 24 h; item 1), and
for `search` the counter `web.SEARCH_FAIL_STREAK >= 2` (each all-providers-failed `web.search` call is one
check), read on every tick without network. `watchdog._bad_streak[row key]` and
`watchdog._last_connector_alert[row key]` (date) live in memory.

One outage, one question: while the `model_server` Doctor row is `bad`, `repeated_error` ignores `error` events
whose message is a model-connection failure (`llm.LLMError` texts "Can't reach the model server", "took longer
than", "dropped the connection", matched after `normalise_error`). That cause is reported by `stalled`, so a
model outage yields exactly one question instead of two.

An escalation is a row in `approvals` with `action='question'`, `agent_id` = the company's CEO, `task_id` when
the pattern names one, `summary` = the question's first 140 chars, and
`payload = {"question", "options": [str], "recommended": str, "context": str, "pattern": key, "task_id": int|null,
"company_id": int}`. `context` holds the evidence (last error text, the latest working-notes entry, Doctor
detail). Dedupe: no new row while a pending `question` with the same `pattern` exists, and none within 6 h of
one being decided (SQL: `action='question' AND payload LIKE '%"pattern": "<key>"%' AND (status='pending' OR
decided_at >= ?)`, so it survives restarts). `watchdog.escalate(company, pattern, question, options, recommended,
context="", task_id=None) -> int | None` (committed signature) creates the row, emits
`bus.emit(cid, "approval", "asked the owner: <question>", ceo_id, {"approval_id", "action": "question", "pattern"})`,
calls `bus.notify("approvals")` and `notify.approval_requested(row)`, and returns the id (None when deduped).

`task_failed(task, reason, run_status)` is authoritative for the task's status after a failed attempt: it
returns immediately for `run_status == "stopped"` (and decrements `attempts`, see item 4); otherwise it sets the
task `blocked` and escalates when `attempts >= max_task_attempts`, else sets it `todo`. The hard-coded `>= 3`
rule in `_finalize` then only matters when the watchdog is missing and can be removed.

`founding_ended(company, run_status, summary)`: `_finalize` keeps activating the company on `done`,
`out_of_steps` and a `partial` finish (committed); `founding_ended` then files `founding_partial:<cid>`. The
company never waits in status `founding` for the owner: the scheduler starts on the first tasks at once, and the
question only asks whether to accept the thin result as it is, retry founding, or pause the company.

Answering. `actions.decide()` keeps today's behaviour for questions (memory note of kind `decision`, a
`waiting`/`blocked` task reopened with the answer appended to its description, `cycle_requested=1`) and the
committed hook calls `watchdog.resolve(row, answer)`, which matches the answer against the options (exact text,
case-insensitive) and applies:

| Answer | Effect |
|---|---|
| `Skip this task` | task status `cancelled`, note "[Owner] skipped via watchdog" appended to `result` |
| `Retry once more with my note` | task status `todo`, `attempts = 0`; the owner's `feedback` text (if any) is appended to `tasks.notes` through `add_note(task_id, feedback, "owner")` |
| `Pause the company` | `companies.status = 'paused'`, event "paused the company" |
| `Retry founding` | `agents.found(cid)` |
| `Continue anyway` | no state change (the company is already active); memory note "Founding accepted as is" |
| `Keep going`, `Keep going without it` | nothing beyond the memory note |
| any other text | treated as a free-text answer; no pattern action, today's behaviour only |

Data. `tasks.reassigned` (committed migration), bumped by `tools.update_task` when `assign_to` changes the role
(owner reassignments through `PATCH /api/tasks/<id>` do not count). Escalations reuse `approvals`.

API. None new. `GET /api/approvals` already returns the payload; the Inbox reads `options`, `recommended`,
`context` and `pattern` from it.

UI. In `requestFields` (app.js), `case 'question'` renders `payload.options` as one `.btn` per option
(`data-act="decide" data-d="answer" data-answer="<option>"`, the recommended one as `.btn primary` with the
suffix "(recommended)") above the free-text answer box, and `payload.context` in a collapsed `<details>` labelled
"Why". `decideRequest` sends `answer = t.dataset.answer || typed answer`. The Overview banner for an active company
with a pending `founding_partial` question says "Founding ended early. A question is waiting in your Inbox." with
a link to `#/inbox`; it disappears when the question is answered.

Agent-facing changes. None directly; the CEO sees the decision in shared memory as today.

Code touch points. Fill `watchdog.tick`, `task_failed`, `founding_ended`, `escalate`, `resolve`; add
`normalise_error(message)`; `repeated_error` skips model-connection errors while `model_server` is `bad` (above).
`agents._finalize`: the task-status block defers to `task_failed`; founding activation stays as committed.
`tools.update_task`: `reassigned = reassigned + 1` when the role changes. `app.js`: option buttons and the
banner text.

### 4. Working notes and retry-differently (`tasks.notes`, `note_progress`)

Behaviour. Every task run offers the committed tool `note_progress(notes)` ("Save working notes: what you tried,
what failed and what is left. They survive if this session ends early, and the next attempt starts from them.").
It calls `watchdog.add_note(task_id, notes, agent name)` and returns "Notes saved." (or "ERROR: this session has
no task." in a cycle or chat run). `add_note(task_id, text, author="")` appends
`--- <author or "note">, <iso time> ---\n<text>` to `tasks.notes` and keeps the newest 4,000 characters, cutting
at an entry boundary when possible. `note_progress` does not count as a workspace change for the loop guard.

When a run ends without a clean finish, the committed `_finalize` appends `agents._auto_note(...)` through
`add_note(..., "auto")`. `_auto_note` (committed) produces:

```
--- auto, 2026-10-14T10:22:05Z ---
Attempt 2 ended: out_of_steps
Files written: docs/notes.md, site/index.html
Last tool calls: write_file({"path": "site/index.html", "content": "<!doctype html><html lang=\"en\"><he); audit_page({"path": "site/index.html"}); write_file({"path": "site/index.html", "content": "<!doctype h)
Last error: ERROR: web search is unavailable right now (duckduckgo: blocked (HTTP 202 challenge); brave: no key)
```

`Attempt N ended: <how>` carries `ctx.finish_status` when the agent called `finish` (so `partial`) or the run
status (`error (<error text>)`, `out_of_steps`, `stopped`). `watchdog.retry_guidance(task)` reads the newest
`auto` entry plus `tasks.result` and classifies the failure:

| Kind | Detected from the latest auto entry | Guidance text |
|---|---|---|
| `repeated_action` | `tasks.result` or the entry contains "repeating the same action" | "Use a different tool or approach for that step; if it cannot be done, finish with status partial and say why." |
| `search_failed` | "Last error:" line starts with "ERROR: web search is unavailable" | "Do not search. Fetch URLs you already know with fetch_url, or skip research and say so in the handoff." |
| `out_of_steps` | "ended: out_of_steps" | "Scope down: do the smallest useful piece first, save it, call note_progress, then finish." |
| `model_error` | "ended: error" | "Keep tool arguments short: write files in sections with append=true, and keep each call under about 2,000 characters." |
| `owner_answer` | `tasks.description` or `tasks.result` contains "Owner answered question" newer than the entry | "Follow the owner's answer above exactly." |
| `partial` | "ended: partial" with none of the above | "Read the open issues in the handoff above and start from the last saved file." |

`retry_guidance` returns "Attempt N-1 failed because <kind in words>. Do not repeat that approach: <guidance>".
The committed `task_prompt` shows "Working notes from earlier attempts (continue from here):\n<notes, last 3,000
chars>" after the handoff notes, and the guidance line when `attempts > 1`.

Counting attempts. `tasks.attempts` still bumps at run start. For `run_status == "stopped"` (pause or kill
switch) `task_failed` decrements it back so the attempt is not counted. A `partial` finish the agent chose
itself counts as a failed attempt, exactly like `error` and `out_of_steps`; only `stopped` is not counted. After
`max_task_attempts` (setting, default 2) failed attempts the task is `blocked` and `task_failed_twice` is
raised; otherwise the task returns to `todo`.

Data. `tasks.notes` (committed migration). Setting `max_task_attempts` (committed, default 2).

API and UI. `GET /api/companies/<cid>/tasks` returns `notes` with the row. The task modal on the Tasks tab shows
"Working notes" as a `<pre class="box">` under the handoff when non-empty. No new route.

Agent-facing changes. `note_progress` in every task run (committed through `roles.TASK_EXTRA`). Prompt text as
above. `agents._shared_rules` gains one line for task runs: "Call note_progress whenever you learn something the
next attempt would need, and before any long step."

Code touch points. Fill `watchdog.add_note`, `watchdog.retry_guidance`, `watchdog.note_progress`. `agents._shared_rules`.
`tests/test_unit.py::TestLoopGuard.run_script` is the pattern to reuse for note tests.

### 5. Telegram and ntfy (`notify.py`)

Behaviour. `notify.send(text, buttons=None, kind="info") -> bool` (committed signature) sends `text` to
Telegram when `telegram_bot_token` and `telegram_chat_id` are set, and to ntfy when `ntfy_topic` is set.
`buttons` is a list of rows, each row a list of `{"text": label, "data": callback_data}`. Telegram messages use
HTML parse mode (Holdco escapes `<`, `>` and `&` in dynamic text) and an inline keyboard built from `buttons`.
ntfy gets plain text with no buttons: one HTTP POST of the message body to `<ntfy_server>/<ntfy_topic>` (verify
against the vendor docs before connecting). Identical text is not resent within 1 h (`notify._recent:
dict[sha1(text), epoch]`). `kind` is `important` or `info`; `info` messages are sent only when
`notify_level == "all"`. `send` returns true when at least one channel accepted the message and never raises
(errors are printed and counted in `notify._failures` for the Doctor `telegram` row).

| Hook | Where it is called | Kind | Text (HTML) | Buttons |
|---|---|---|---|---|
| New approval request | `actions.gate` → `notify.approval_requested(row)` (committed hook), rows other than questions | important | `<b>Hunter</b> at <b>PawPlan</b> wants to send an email\nTo: jo@…\nSubject: …\n<first 600 chars of the body or content>` | `Approve` → `a:<id>:approve`, `Reject` → `a:<id>:reject` |
| Question or escalation | same hook for `question` rows (from `ask_owner` or `watchdog.escalate`) | important | the question, then `Recommended: <option>` when the payload has one, and the context's first 400 chars | one button per option → `q:<id>:<option index>`; a plain `ask_owner` question with no options gets no buttons and the text "Answer it in the dashboard" |
| Error | `bus.emit` when `kind == "error"` → `notify.error(company, message)` (committed function; the call site is new) | important | `<b>Error</b> at <b>PawPlan</b>: <message>` | none; one per distinct `watchdog.normalise_error` message per hour (`notify._errors`) |
| Founding finished | `agents._finalize` (founding `done`) | important | `<b>PawPlan</b> is founded. <one-liner>. First tasks are queued.` | none |
| Kill switch toggled | `server.api_kill` (committed call), `/stop`, `/resume` | important | "Kill switch ON. Agents stopped, outbound actions frozen. Send /resume to turn it off." / "Kill switch off. Agents may work again." | none |
| Task finished, owner report | `agents._finalize` `done` events, `tools.report_to_owner` | info | `<b>Forge</b> finished task #12: <summary>` | none |
| Daily brief | Phase 2 | important | see [phase-2-organize.md](phase-2-organize.md) | none |

Long polling. `notify.Poller` is a daemon thread started by `notify.ensure_poller()` (committed signature, called
every tick from `_housekeeping`) when token and chat id are both set, and stopped when either is cleared. It
calls the Bot API's `getUpdates` with `timeout=25`, `offset` = last update id + 1, and, if the Bot API lets a
client narrow the update types, only messages and callback queries (verify against the vendor docs); on any
exception it sleeps with backoff (2 s doubling to 60 s) and continues.
Updates from any chat id other than `telegram_chat_id` are ignored. Callback data `a:<approval_id>:approve|reject`
calls `actions.decide(approval_id, decision)`; `q:<approval_id>:<option index>` calls
`actions.decide(approval_id, "approve", answer=options[index])`. After `decide` the poller answers the callback
query and edits the original message to append `\n\nYou: Approved` (or Rejected, or the option text) and removes
the keyboard; on `ValueError` (already decided, kill switch) the callback answer carries the error text. Text
commands: `/status` replies with one line per non-archived company (`PawPlan: 7 open tasks, 2 working, 1 request
waiting`) plus the Doctor summary; `/stop` turns the kill switch on; `/resume` turns it off (both through the
same code as `POST /api/kill`). Any other text gets "Commands: /status, /stop, /resume. Answer requests with the
buttons." The poller keeps running while the kill switch is on.

Data. Settings (committed): `telegram_bot_token`, `telegram_chat_id`, `telegram_api_base` (default
`https://api.telegram.org`), `ntfy_topic`, `ntfy_server` (default `https://ntfy.sh`), `notify_level`. In this
phase the bot token is stored in the settings table like `brave_api_key` is today; Phase 1 moves it into the
vault. `notify._recent`, `notify._errors`, `notify._failures`, `notify.POLLER` in memory.

Bot API calls are built as `<telegram_api_base>/bot<token>/<method>` with a JSON body. The method names are kept
in one table, `notify.METHODS`, so a correction is a one-line change: `getMe` and `getUpdates` are named in the
brief; sending a message, answering a callback query and editing message text are described in the Telegram Bot
API (verify the exact method and field names against the vendor docs before connecting).

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/notify/test` (committed route) | `{}` | `{"ok": bool, "message": str, "chat_ids": [int]}`. Sends "Holdco test message" with one `OK` button (callback `noop`). When the token is set but the chat id is empty it calls `getUpdates` once and returns the chat ids seen, with the message "Send any message to your bot in Telegram, then press this again and copy your chat id." |

UI. Settings page: a new `.section` "Phone notifications" between Autopilot and Approvals with fields Telegram
bot token (password input), Telegram chat id, ntfy topic, ntfy server, "Notify me about" (`important` / `all`),
and a `Send test message` button (`data-act="test-notify"`) that saves the form first (as `test-llm` does) and
shows the result inline. `telegram_api_base` is not shown; it is settable through `PUT /api/settings` for tests.

Agent-facing changes. None.

Code touch points. Fill `notify.send`, `approval_requested`, `error`, `ensure_poller`, `api_notify_test`; add
`check_telegram(settings)`, `Poller`, `format_status()`, `METHODS`. `bus.emit`: `if kind == "error": notify.error(...)`
(lazy import, exceptions swallowed). `agents._finalize`: founding and `info` hooks. `app.js`: the Settings section.

### 6. Kill switch (committed)

Behaviour. Setting `kill_switch` (bool, default false). When on:

- `scheduler.tick` returns right after `_housekeeping`: no cycles, no routines, no task dispatch.
  `scheduler_enabled` is not changed.
- `agents.run` checks the setting at every step boundary for every kind except chat and ends with status
  `stopped`, error "Kill switch: the owner stopped all agents." Tasks go back to `todo`; the attempt is not
  counted (item 4).
- `actions.gate` treats policy `auto` as `approve`; the implementation changes the tool result for that case to
  "Kill switch is on: queued for the owner's approval as request #N." Policy `off` still refuses.
- `actions.decide` with `decision == "approve"` on any row whose action is not `question` raises
  `ValueError("The kill switch is on. Turn it off to run approvals.")`. Rejections and answers to questions
  still work, so escalations can be answered while stopped.
- The Telegram poller keeps running so `/resume` works.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/kill` (committed) | `{"on": bool}` (default true) | `{"kill_switch": bool}`; emits an `info` event with `{"by": "owner"}`, `bus.notify("settings")`, and `notify.send(..., kind="important")` |

UI. `/api/state.kill_switch` drives: a red `Stop everything` button (`.btn danger`, `data-act="kill"`) in the
sidebar footer under the Autopilot engine when off; when on, the button reads `Resume everything` (`.btn
primary`) and `render()` prepends a `.banner bad` to every page: "Kill switch is on. No agent runs, nothing is
sent or published, approvals cannot be executed." with a `Resume` button. Both post `/api/kill`.

Agent-facing changes. A stopped run sees nothing; the next attempt sees the `stopped` note entry.

Code touch points. Remaining: the gate's tool-result text, `app.js` (`renderSide`, `render`, click handler for
`kill`).

### 7. Backups (`backup.py`)

Behaviour. `backup.run_backup() -> Path` writes `backup.BACKUP_DIR / holdco-YYYYMMDD-HHMMSS.zip` (UTC stamp;
`BACKUP_DIR = DATA_DIR / "backups"` is committed) containing:

| Zip entry | Source | Notes |
|---|---|---|
| `holdco.db` | `db.backup_to(tmp_path)`: `sqlite3.Connection.backup` from the shared connection into a temp file, under `db._lock` | consistent even with WAL and writers active |
| `companies/**` | `config.COMPANIES_DIR` | skips directories named `.runs`, `.trash`, `__pycache__`, `node_modules` |
| `vault.json`, `vault.key` | `config.DATA_DIR` | Phase 1; skipped when absent |

After writing, it deletes all but the newest `backup_keep` (committed setting, default 14) files matching
`holdco-*.zip`, emits `bus.emit(None, "info", "Backup written: holdco-….zip (12.3 MB)")`, and returns the path.
Failures raise; `maybe_run` catches them and emits an `error` event (so they reach the phone).
`backup.last_backup() -> {"path", "created_at", "size"} | None` (committed signature) reads the newest file.
`backup.maybe_run()` (committed signature, every tick) starts `run_backup` in a daemon thread when the newest
backup is older than 24 h or none exists, checked at most every 10 minutes and never while a backup thread is
running. Restore is manual and documented in the README: stop Holdco, unzip the archive over the data folder
(replacing `holdco.db` and `companies/`), start Holdco.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/backup` (committed route) | `{}` | `{"name": str, "size": int, "created_at": iso}` (runs synchronously) |
| GET | `/api/backups` (committed route) | none | `{"folder": str, "backups": [{"name", "size", "created_at"}], "keep": int}` newest first |

UI. The Doctor page `backup` row's fix button "Back up now". The Settings page gets a "Backups" `.section` with
the folder path, "Keep the newest N backups" (`backup_keep`), a `Back up now` button and the list from
`GET /api/backups`.

Agent-facing changes. None.

Code touch points. Fill the five functions in `backup.py`; add `db.backup_to(path)`.

### 8. Audit log (committed)

Behaviour. Every outbound action that executes is written to the `audit` table by `actions.audit(...)`, whether
it ran under policy `auto` (`approved_by = "policy:auto"`) or after the owner's approval (`approved_by = "owner"`),
and whether it succeeded or failed (`result = "FAILED: <error>"`). Rows are never updated or deleted; no route
modifies them. Phase 1 writes `connector_call` rows through the same function.

Table `audit` (committed): `id, created_at, company_id, action, proposed_by (agent name), approved_by, payload
(exact JSON sent, after owner edits, sorted keys, <= 20,000 chars), result (<= 4,000 chars), approval_id`.
Index `ix_audit_company(company_id, id)`.

| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/api/audit` (committed) | `company_id` (optional), `limit` (default 100, max 500) | `{"audit": [rows with company_name, payload parsed]}` newest first |

UI. Inbox page: a third `.panel` "Audit log" under Recent decisions, listing `created_at`, company, action label
(`actions.ACTION_LABELS`), `proposed_by`, `approved_by`, a one-line summary (`actions._summary(action, payload)`
is server-side; the client builds the same from the payload), and the result's first 200 characters; a
`<details>` reveals the payload.

Agent-facing changes. None.

Code touch points. Remaining: the Inbox panel in `app.js`.

### 9. Settings (committed)

Additions to `config.DEFAULT_SETTINGS`, all editable through `PUT /api/settings` (the allow-list in
`db.set_settings` is derived from `DEFAULT_SETTINGS`):

| Key | Default | Used by |
|---|---|---|
| `telegram_bot_token` | `""` | notify (secret; vault from Phase 1) |
| `telegram_chat_id` | `""` | notify |
| `telegram_api_base` | `"https://api.telegram.org"` | notify; tests point it at the fake |
| `ntfy_topic` | `""` | notify |
| `ntfy_server` | `"https://ntfy.sh"` | notify |
| `notify_level` | `"important"` | notify (`important` or `all`) |
| `kill_switch` | `False` | scheduler, agents, actions |
| `max_task_attempts` | `2` | watchdog |
| `backup_keep` | `14` | backup |

Remaining: `server.api_settings_put` validates `notify_level` against its two values, `max_task_attempts` 1..5,
`backup_keep` 1..90, and `telegram_chat_id` as digits with an optional leading minus.

## Files

New:

- `holdco/routing.py` (committed): route registry.
- `holdco/doctor.py` (skeleton committed): checks, cache, preflight, `/api/doctor` routes.
- `holdco/watchdog.py` (skeleton committed): patterns, escalation rows, resolve actions, working notes,
  `note_progress`.
- `holdco/notify.py` (skeleton committed): Telegram and ntfy sending, long-polling thread, `/api/notify/test`.
- `holdco/backup.py` (skeleton committed): zip backups, retention, `/api/backup` and `/api/backups`.
- `tests/fake_telegram.py`: fake Bot API server (contract below), also serves fake ntfy and a SearXNG-shaped
  `/search` endpoint for the e2e search chain.
- `tests/test_phase0.py`: unit tests for every item.
- `docs/phases/phase-0-stabilize.md`: this file.

Changed:

- `holdco/config.py` (committed): nine settings.
- `holdco/db.py` (committed: `audit` table and index, `tasks.notes`, `tasks.reassigned`); to do: `backup_to()`.
- `holdco/web.py`: provider chain, `PROVIDER_STATUS`, `LAST_PROVIDER`, `SEARCH_FAIL_STREAK`, `LAST_SEARCH_ALERT`,
  `HOLDCO_SEARCH_PROVIDERS`.
- `holdco/llm.py`: `loaded_models()`, `tool_ping()`.
- `holdco/emailer.py`: `check_smtp()`, `check_imap()`.
- `holdco/tools.py` (committed: `watchdog` import at the bottom); to do: `web_search` message, `update_task`
  reassigned counter.
- `holdco/roles.py` (committed): `TASK_EXTRA`.
- `holdco/agents.py` (committed: hooks, `_auto_note`, kill switch, `task_prompt` notes, founding activation on
  `done`, `out_of_steps` and `partial`); to do: founding prompt search-down branch, `_shared_rules` line, notify
  hooks in `_finalize`.
- `holdco/actions.py` (committed: kill switch, audit, notify and watchdog hooks); to do: kill-switch tool text.
- `holdco/scheduler.py` (committed: `_housekeeping`, kill switch); nothing left to do in this phase.
- `holdco/server.py` (committed: routing import, preflight, `/api/kill`, `/api/audit`, state fields); to do:
  `api_llm_test` via `llm.tool_ping`, settings validation.
- `holdco/bus.py`: error hook to notify.
- `holdco/static/app.js`: Doctor view and sidebar entry, New company 409 handling, Inbox option buttons and
  audit panel, Settings sections (Phone notifications, Backups), kill switch button and banner, task modal notes,
  Overview founding banner text.
- `holdco/static/style.css`: `.dot.bad`, doctor row layout, option button row.
- `README.md`: Doctor, phone notifications, kill switch, backups and restore, new settings.
- `tests/test_e2e.py`, `tests/fake_ollama.py`: additions below.

## Tests

All tests run with `python -m unittest discover -s tests -t .`, no network, Windows and Linux, Python
3.10/3.12/3.13.

### Unit tests (`tests/test_phase0.py`)

The file follows `tests/test_unit.py`: `HOLDCO_DATA` set to a temp folder before import, `db.init()`,
`make_company()` and `ctx_for()` copied or imported, `FakeModelServer` from `tests/fake_ollama.py` started in
`setUpClass` where a model server is needed.

| Class | Tests |
|---|---|
| `TestDoctor` | rows have the schema; `model_server` bad when `llm_base_url` points at a closed port, ok against `FakeModelServer`; `model` missing vs installed; `gpu_fit` warn when a fake `/api/ps` (add to `fake_ollama`) reports `size_vram < size`; `disk`, `backup` (none → bad; after `run_backup` → ok), `errors_24h` thresholds; connector rows appear only when configured and `conn:<cid>:webhook` flags a malformed URL; `preflight()` returns `checks`/`soft`/`ok`; `search_down()` follows `PROVIDER_STATUS`; `refresh_if_stale` runs once and not while a refresh is in flight; an `smtp` row that has been ok for 24 h (`_ok_since` set back) is not re-run on the next hourly refresh (`emailer.check_smtp` patched to count calls) and is re-run by `run(force=True)` |
| `TestSearchChain` | monkeypatch `web._brave/_searxng/_ddgs/_ddg_html`: configured provider first; a raising provider is skipped and recorded with `cooldown_until`; cooling providers are skipped without being called; first non-empty wins and `LAST_PROVIDER` is set; all failing raises the exact "All search providers failed: …" text in chain order and bumps `SEARCH_FAIL_STREAK`; `_ddg_html` raises "blocked (HTTP 202 challenge)" on a 202 anomaly page served by a local `ThreadingHTTPServer`; `tools.web_search` returns the agent message and emits one `error` event, and a second failure within 30 min emits none; `HOLDCO_SEARCH_PROVIDERS` restricts `_order` |
| `TestWatchdog` | each pattern creates exactly one `question` row with the right `pattern`, options and recommended; a second `tick()` creates none; a decided question is not recreated within 6 h; `task_failed` sets `todo` below `max_task_attempts` and `blocked` at it, and a self-chosen `partial` counts like `error`; `founding_ended` files the question while the company is already `active`, and `Continue anyway` changes no status; `resolve()` applies each option; `normalise_error` strips names and digits; `repeated_error` ignores model-connection errors while the `model_server` row is `bad`; `task_failed` with `stopped` neither counts nor escalates |
| `TestNotes` | `note_progress` appends through `add_note` and the cap holds at 4,000 chars; `_finalize` writes the auto entry with files, last 3 calls and the ended line (reuse `TestLoopGuard.run_script`); `retry_guidance` returns the right guidance for each kind; `task_prompt` shows notes and the guidance line; a `stopped` run does not bump `attempts` |
| `TestNotify` | with `FakeTelegram`: `send` posts HTML text and an inline keyboard; identical text within 1 h is dropped; `info` kind is dropped at level `important`; `approval_requested` has Approve/Reject buttons for a request and one button per option for a watchdog question; errors rate-limited per message per hour; the poller handles `a:<id>:approve` (row done, callback answered, message edited), `q:<id>:1` (answered with the option text), ignores another chat id, answers `/status`, toggles the kill switch on `/stop` and `/resume`; ntfy receives plain text; `/api/notify/test` returns `chat_ids` when the chat id is empty |
| `TestKillSwitch` | `gate` queues an `auto` action with the "Kill switch is on" text; `decide(approve)` raises the exact message, `decide(reject)` and question answers still work; `agents.run` ends `stopped` with the kill-switch error (run_script); the task stays `todo` with `attempts` unchanged |
| `TestBackup` | the zip contains `holdco.db` and company files and skips `.runs`; the copied database opens and lists the companies; retention keeps `backup_keep`; `last_backup()`; `maybe_run` starts one thread and not a second while it runs |
| `TestAudit` | an `auto` social post writes a row with `approved_by='policy:auto'`; an approved one writes `owner`; a failed email writes `FAILED: …`; `GET /api/audit` filters by company; `routing.ROUTES` has no PUT/PATCH/DELETE on `/api/audit` |

### E2E additions (`tests/test_e2e.py`)

`setUpClass` also starts `FakeTelegram` and sets `telegram_api_base`, `telegram_bot_token="t"`,
`telegram_chat_id="42"`, `ntfy_server=<fake>/ntfy`, `ntfy_topic="holdco"`, `search_provider="searxng"` and
`searxng_url=<fake>/search` through `PUT /api/settings`, and starts Holdco with
`HOLDCO_SEARCH_PROVIDERS=searxng` so no test touches DuckDuckGo. `FakeTelegram` also serves a SearXNG-shaped
`GET /search?q=&format=json` with two results, so the fake model's founding and Scout searches succeed (the
founding script calls `web_search`); only `test_19` breaks search, and it restores the URL afterwards. `tests/fake_ollama.py`
gains two scripted agents: `Flaky` (a custom role whose script never calls `finish`, so every run ends
`out_of_steps`) and a founding script variant that calls `finish(status="partial")` when the idea contains
"partial-founding". New tests, numbered after the existing ones:

- `test_16_doctor_and_preflight`: `GET /api/doctor` returns `model_server` ok; set `llm_base_url` to a closed
  port, `POST /api/companies` → 409 with `checks`; same body with `force: true` → 200; restore the URL.
- `test_17_task_failed_twice_reaches_the_phone`: create a task for `Flaky`; wait until the task is `blocked`;
  exactly one pending `question` with pattern `task_failed_twice:<id>`; `FakeTelegram.sent` has one message
  with 3 buttons; push the callback for option 0 through the fake; the task becomes `cancelled` and the fake
  recorded one callback answer and one edit.
- `test_18_founding_partial`: found with "partial-founding" in the idea; the company is `active` as soon as the
  founding run ends and one question with pattern `founding_partial:<cid>` is pending; answer `Continue anyway`
  over the API → the question is `answered`, the company is still `active`, and a memory note "Founding accepted
  as is" exists.
- `test_19_search_outage_and_model_outage`: point `searxng_url` at a closed port (`PUT /api/settings`); two
  agent searches (two research tasks for Scout) → one `connector_down:search` question and one Telegram message;
  restore `searxng_url`. Then stop the fake model server, `POST /api/doctor/run` so the `model_server` row is
  `bad`, set `runs.started_at` back 2 h for the company's runs with `sqlite3` (as `test_08` edits
  `companies.created_at`), wait 3 ticks → one `stalled:<cid>` question, one error notification, and no
  `repeated_error` question and no duplicates after 3 more ticks; restart the fake model.
- `test_20_kill_switch_and_backup`: `POST /api/kill {"on": true}` → `/api/state.kill_switch` true, no new runs
  in 3 s, approving a pending request → 400 with the kill-switch text; `/resume` through the fake poller turns
  it off; `POST /api/backup` → file listed by `GET /api/backups`; `GET /api/audit` has the rows from `test_05`.

### Fake Telegram contract (`tests/fake_telegram.py`)

`FakeTelegram(port=0, chat_id=42)` is a `ThreadingHTTPServer` with `.url` (use as `telegram_api_base`),
`.start()`, `.stop()`, and these records: `sent: list[dict]` (every send body, with `message_id` assigned),
`answered: list[dict]` (callback answers), `edited: list[dict]` (edit bodies), `ntfy: list[dict]`
(`{"topic", "body"}`), `searches: list[str]` (queries received by `/search`). Test hooks: `push_callback(data: str, message_id: int | None = None, chat_id=None)` and
`push_text(text: str, chat_id=None)` queue updates for the next `getUpdates`. It accepts any token in the path.

| Path (under `/bot<token>/`) | Behaviour | Response |
|---|---|---|
| `getMe` | | `{"ok": true, "result": {"id": 1, "is_bot": true, "username": "holdco_test_bot"}}` |
| `getUpdates` | honours `offset` (drops updates with a smaller `update_id`) and `timeout` (waits up to `min(timeout, 2)` s for a queued update, returns early when one arrives) | `{"ok": true, "result": [update, ...]}` |
| send message | records the body (`chat_id`, `text`, `parse_mode`, `reply_markup`) | `{"ok": true, "result": {"message_id": n, "chat": {"id": chat_id}, "text": text}}` |
| answer callback query | records `callback_query_id`, `text` | `{"ok": true, "result": true}` |
| edit message text / reply markup | records the body | `{"ok": true, "result": {"message_id": …}}` |
| anything else | | `404 {"ok": false, "error_code": 404, "description": "Not Found"}` |
| `POST /ntfy/<topic>` (outside `/bot…`) | records topic and raw body | `200 {"id": "x"}` |
| `GET /search?q=&format=json` (outside `/bot…`) | SearXNG-shaped search used as the e2e search provider; records `q` in `searches` | `200 {"results": [{"title", "url", "content"}, {"title", "url", "content"}]}` |

The three method paths that the brief does not name must match the real Bot API method names used by
`notify.METHODS` (verify against the vendor docs); the fake imports `notify.METHODS` so the two cannot drift.

Update shapes the fake produces (verify field names against the vendor docs; Holdco reads only the fields shown):

```
{"update_id": 1001, "callback_query": {"id": "cq-1", "from": {"id": 42, "is_bot": false, "first_name": "Owner"},
 "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}, "text": "…"}, "data": "a:12:approve"}}

{"update_id": 1002, "message": {"message_id": 8, "from": {"id": 42, "is_bot": false},
 "chat": {"id": 42, "type": "private"}, "text": "/status"}}
```

## Gate

From the plan: "a new company founds with no help from you, and any failure reaches your phone within 5 minutes
as one clear question." From the brief, tested with fakes:

| Gate clause | Proof in the suite |
|---|---|
| Founding needs no help | `test_03_found_company_and_autopilot` unchanged: the fake model founds PawPlan and the first tasks finish with no owner action; `test_16` shows the preflight blocks only on a hard failure |
| Founding that ends partial → one question + one Telegram message with buttons, within the next tick | `test_18_founding_partial`, `TestWatchdog` |
| Task failing twice → same | `test_17_task_failed_twice_reaches_the_phone`, `TestWatchdog`, `TestNotes` |
| Search outage → same | `test_19` first half, `TestSearchChain` |
| Model server outage → same | `test_19` second half (`stalled`), `TestWatchdog`, `TestDoctor` |
| Pressing a button answers it | `test_17` (callback → decide → task cancelled), `TestNotify` |
| Exactly one | the dedupe assertions in `test_17`, `test_19` and `TestWatchdog` |
| Suite green on Windows and Linux | existing CI matrix; `fake_telegram` and `fake_ollama` bind 127.0.0.1 only |

Timing on a real PC: `_housekeeping` runs every `TICK_SECONDS` (8 s) and `notify.send` is synchronous, so a
pattern reaches the phone within one tick of becoming true. The 5-minute bound in the plan is comfortably met;
what it does not cover is the time a failing attempt takes to fail (bounded by `max_steps_per_run` and
`request_timeout`).

## How to try it on your PC

1. Check out `claude/phase-0-stabilize`, start Holdco (`start_windows.bat` or `./start_linux.sh`), open
   `#/doctor`. With Ollama stopped the first two rows are red; start Ollama and press `Run checks`.
2. Create a Telegram bot with Telegram's bot-creation flow (BotFather) and copy the token (verify the steps
   against the vendor docs). In Settings → Phone notifications paste the token, leave the chat id empty, press
   `Send test message`: it says to message the bot. Send "hi" to your bot in Telegram, press the button again,
   copy the chat id it reports into the field, save, press again: the test message with an `OK` button arrives.
3. Optional: install the ntfy app, pick a topic name, enter it; the test message arrives there too.
4. Stop Ollama, go to `#/new`, describe an idea: the page shows the red model-server check and `Found anyway`.
   Start Ollama, press `Found this company`. Within a minute "PawPlan is founded" arrives on the phone, then
   approval requests with `Approve`/`Reject` buttons as the team works. Approve a social post from the phone;
   the Inbox and the audit log show it.
5. Force a failure: on the Tasks tab add a task "Fetch https://this-host-does-not-exist.invalid and summarise
   it" for Research with acceptance "- summary of the page". After two attempts the task is blocked and one
   question with three buttons arrives. Press `Skip this task`.
6. Disconnect Wi-Fi and chat to the CEO "research competitors": one "Web search is down" error reaches the phone,
   the Doctor `search` row turns red, and after a second failed search a `connector_down:search` question
   arrives. Reconnect.
7. Send `/stop` to the bot: the red banner appears in the dashboard and runs end with the kill-switch note. Send
   `/resume`.
8. Press `Back up now` on the Doctor page, then look in `data/backups/`.

## Open questions

None left. The three questions earlier drafts raised were decided and are folded into the items above: SMTP and
IMAP logins back off to every 6 h once a connector has been ok for 24 h (item 1); a founding that ends partial
activates the company first and asks afterwards, so `Continue anyway` only acknowledges (item 3); and a
`partial` finish the agent chose itself counts toward `max_task_attempts` like an error (item 4).
