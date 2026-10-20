# System design (as built)

How Holdco works today, after Phases 0 to 4. `docs/architecture.md` describes the original state and the target;
this file describes the code. Module names are under `holdco/`.

## Process model

`run.py` starts one Python process:

- **HTTP server** (`server.py`, `ThreadingHTTPServer`, default 127.0.0.1:8420). Serves `static/`, the JSON API under
  `/api/` (routes from `routing.route` in `server.py` and feature modules), Server-Sent Events for live updates
  (`bus.py`), company site previews at `/preview/<slug>/`, and MCP at `/mcp` (`mcp_server.py`). Basic auth when
  `HOLDCO_PASSWORD` is set (required to bind beyond localhost); writes need the `X-Holdco` header (CSRF); `/mcp`
  instead refuses a foreign `Origin`.
- **Scheduler thread** (`scheduler.py`), one `tick()` every `HOLDCO_TICK_SECONDS` (8 s by default).
- **Telegram poller** (`notify.py`, long polling, so no port is opened) and a sender thread for phone messages.
- **Agent runs**: each session is a thread started by `agents.start()`. `max_concurrent_runs` caps sessions; the GPU
  slot (`llm.background_slot`) is taken per model call, so a chat or follow-up gets the model at the next step.
- **Background helpers**: Doctor refreshes, connector health checks and metric polls, the portfolio brief, index
  embedding. None of them may block `tick()`.

Storage: one SQLite file `data/holdco.db` (`db.py`: one connection, a lock, WAL), one folder per company under
`data/companies/<slug>/` (`workspace.py` keeps agents inside it), the vault (`vault.py`), backups.

## The scheduler tick

`Scheduler.tick()` in order: inbox polling, housekeeping (watchdog, Doctor, backups, phone poller, connector checks
and polls, decision deadlines, the daily portfolio brief, the monthly portfolio review), then it stops if the kill
switch is on, the model server is down, autopilot is off or a Bench run holds the GPU. Then:

1. Phase 3 dispatch classes 1 and 2 (owner follow-ups, tasks due within 24 h) go first.
2. CEO planning cycles when due (change-driven, Phase 0.5: no cycle while nothing changed).
3. Routines are created even when the company is over budget or the scheduler is full.
4. The rest of the queue, by class (p1, normal, routines, low-value research in quiet hours), reordered inside
   classes 3 to 6 by each company's GPU share (`portfolio.order`, Phase 4). Phase 2 rules hold throughout:
   dependencies done, daily token and GPU budgets (owner tasks exempt).

## An agent session (`agents.run`)

1. **Context.** `system_prompt()` joins, in order: the preset's role text (`presets.render`) with the agent's focus,
   the facts block (`truth.facts_section`), the company block (`brain.company_section`: the brain digest, or the
   charter core before the brain exists), the business playbook, the owner's pinned context, what changed since
   this agent's last session, what waits on the owner, connectors that are down, relevant memory, metrics (only
   while the brain is empty) and the files index. The user message is the task prompt (with the task contract,
   rhythm data or working notes), the cycle prompt, a founding stage prompt or the chat text.
2. **Loop.** Up to `max_steps` model calls (founding stage budgets, cycles at least 18, chat at most 8, tasks by
   `contracts.max_steps_for`). `llm.chat` returns tool calls (native, or prompt-based JSON shaped by a schema).
   Calls written as text, wrapped calls (`functions.x`, `container.exec`) and aliases are recovered. A loop guard
   stops identical repeated calls.
3. **Tools.** `tools.execute` resolves aliases and argument renames, validates arguments against the schema (repair
   messages, coercion of unambiguous values), runs the tool inside `taint.current(ctx)` and fences outside content.
4. **Finish.** `finish` runs, in order: founding stage checks, the waiting check, a partial finish naming a missing
   file, the brief or deliverable check, the verification gate (`verify.finish_problem`), the claims gate
   (`truth.finish_guard`). Each pushes back once, then the status is downgraded and the reason noted.
5. **Finalize.** `_finalize` stores the run (status, summary, tokens, `max_prompt_tokens`), updates the task (status,
   result, working notes), releases file leases, adds tokens to the task budget, runs the weekly clean-up after a
   weekly review, tells the watchdog about failures, and moves founding to its next stage.

## An outbound action

```
agent tool (send_email, social_post, deploy_site, http_post, connector call, run_code, spend_money)
  -> guards in the tool (truth.outreach_guard, verify.outreach_check, deploy_guard, daily caps)
  -> actions.gate(company, agent, task_id, action, payload, policy_action)
       locked (spend_money, irreversible) -> ask, always
       policy off -> refused
       trust.gate_policy: tainted session -> ask; legal wording / first contact -> ask;
                          trust level 2 -> run; level 1 -> run under its daily cap; else the policy
       kill switch on -> queue instead of run
       run now -> actions.execute + audit (approved_by "policy:auto" or "trust:<level>")
       ask    -> approvals row (deduplicated), phone message with buttons
  -> owner decides (dashboard, Telegram, MCP) -> actions.decide
       claims the row atomically, executes, audits (approved_by owner / mcp:<client>), updates the trust streak,
       reopens the waiting task (followup_at), writes a memory note, asks the CEO to re-plan
```

## Data model (tables in `db.SCHEMA`)

| Group | Tables |
|---|---|
| Core | `settings`, `companies`, `agents`, `tasks`, `runs`, `messages`, `memory`, `events` |
| Owner's inbox and audit | `approvals` (requests, questions, decisions), `audit` |
| Outside world | `emails`, `metrics` (with `source`), `fetched`, `facts`, `connectors`, `suppression` |
| Search | `index_state`, `search_vectors` (plus an FTS5 virtual table when available) |
| Organization (Phase 2) | `strategy`, `goals`, `contacts`, `decisions`, `experiments`, `lessons`, `brain_changes`, `leases`, `briefs` |
| Verify (Phase 3) | `bench_runs`, `trust` |

Columns added after the first schema are in `db.MIGRATIONS` (applied at start).

## Extension points

| To add | Do this |
|---|---|
| A tool | `@tool(name, description, props, required, risk=...)` in the owning module (`tools.py` imports feature modules at its bottom); add it to preset belts in `presets.py`; a fake for anything outside |
| An API route | `@route("GET", r"/api/...")` in the owning module; the module must be imported by `server.py` |
| A connector | a `conn_<name>.py` exporting `DEFINITIONS`, listed in `connectors.MODULES`; health, tools with risk tags, metrics with `source`; a `tests/fake_<name>.py` |
| A preset or routine | `presets.py` (tool belt caps are enforced by `test_unit.TestPresets`) |
| A setting | `config.DEFAULT_SETTINGS` or `DEFAULT_COMPANY_SETTINGS`, the coercion in `server.api_company_patch`, a field in `app.js` |
| A housekeeping job | the list in `Scheduler._housekeeping` (never a model call in the tick; use a thread) |
| A bench scenario | `bench/scenarios.py`, a right and a lazy script in `tests/fake_ollama.py` |

## Trust boundaries

- **Agent text is untrusted.** Claims are checked against records (claims gate); outside content is fenced and
  taints the session; inbound email subjects cannot set flags.
- **Files.** `workspace.safe_path` keeps agents in their company folder; `records/` is read-only to agents; deploys,
  previews and the sandbox never follow links out of the folder.
- **Network.** `web.check_public_url` blocks this PC and the home network (tests open `127.0.0.1` with
  `HOLDCO_ALLOW_LOCAL`); relay services are blocked.
- **Secrets.** In the vault, never in tables, events, transcripts, `records/`, MCP results or the audit log.
- **Owner surfaces.** Dashboard (password, CSRF header), Telegram (only the owner's chat id), MCP (password,
  Origin check). Every surface decides through `actions.decide`, so the rules are the same.

## Where each phase lives

| Phase | Modules it added or reshaped |
|---|---|
| 0 Stabilize | `doctor`, `watchdog`, `notify`, `backup`, `routing`, the search chain in `web` |
| 0.5 Ground truth and context | `truth`, `context`, `planning`, `playbooks`, staged founding |
| 1 Connect | `vault`, `mcp`, `oauth`, `connectors`, `conn_stripe/resend/playwright/buffer/brave`, `browser` |
| 2 Organize | `brain`, `contracts`, `rhythms`, `decisions`, leases in `workspace`, budgets |
| 3 Verify | `bench/`, `verify`, `sandbox`, `taint`, `trust`, `embeddings`, structured calls in `llm`/`tools`, dispatch classes |
| 4 Scale | `mcp_server`, `portfolio`, `conn_porkbun/cloudflare/github/plausible/forms/n8n/later` |
