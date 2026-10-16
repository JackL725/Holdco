# Phase 2: Organize

Phase 2 gives every company one structured source of truth (the brain), makes goals measurable, turns tasks into
contracts with dependencies and file leases, adds the daily and weekly rhythms that produce a one-page brief and
a board memo, separates decisions from approvals, and puts a budget on GPU time, steps, emails and posts. After
this phase the CEO plans top down (goal, bets, tasks), every open task names the goal it serves, and the weekly
review is built from connector data instead of agents' own summaries. This document is the implementation spec:
an engineer builds from it, and the design rationale lives in [operating-model.md](../operating-model.md).

Status: built. Branch `claude/phase-2-organize`, based on the code of Phases 0, 0.5 and 1 (pull request
JackL725/Holdco#4 brings 0.5 and 1 into `main`). Gate on the owner's GPU (gpt-oss:20b, a sandbox with a fake Stripe in
test mode): after the weekly review no open agent task lacked a goal, and the weekly memo's numbers came from Stripe
rows, with the hand-typed waitlist number listed under self-reported only. Depends on Phase 0
([phase-0-stabilize.md](phase-0-stabilize.md)), Phase 0.5
([phase-0.5-ground-truth-and-context.md](phase-0.5-ground-truth-and-context.md)) and Phase 1
([phase-1-connect.md](phase-1-connect.md)). See also [architecture.md](../architecture.md),
[owner-experience.md](../owner-experience.md) and [testing.md](../testing.md).

What Phase 0.5 and Phase 1 change in this design, decided at the start of the build:
- Founding is staged (research, charter, critic, plan). The charter stage calls `set_strategy` and `set_goal` after
  `update_charter`; the plan stage gives every first task a `goal_id` and `expected`.
- The company block in the system prompt comes from `brain.company_section`: the digest once the brain has content,
  otherwise Phase 0.5's charter core. The facts block (`truth.facts_section`) stays above it.
- The planning prompt keeps Phase 0.5's header (nothing changed, waiting on the owner) first, then this phase's goals,
  experiments, proposals and decisions due (`contracts.cycle_sections`), then the existing sections.
- `ask_decision` reuses Phase 0.5's one-question-per-topic rules and the claims gate; decisions count toward the cap
  of pending agent questions.
- `gpu_hours_per_day` sits beside Phase 0.5's `daily_token_budget`; either stops dispatch and cycles for the day.
- Role belts get `search`, `grep`, `edit_file`, `company_status` and `find_tool` at run time (Phases 0.5 and 1) on top of
  the preset lists that `test_integrity` counts.
- `write_brief` refuses a brief that states a number missing from its data block (once, listing the numbers), the same
  anti-fabrication rule as Phase 0.5's claims gate.
- End-to-end tests are numbered from `test_30`. Code lives in `brain.py`, `contracts.py` (task contracts, dependency
  graph, budgets), `rhythms.py` and `decisions.py`; leases in `workspace.py`.

Changes made after the first real-model run (gpt-oss:20b on the owner's GPU, `tests/test_phase2_realmodel.py`):
- `create_task` does not offer `budget_steps` or `budget_tokens` to agents: the model set 3 steps and 200 tokens on
  every task it created, which would have starved them. Budgets stay on the owner's task form and in the API.
- A tool call whose arguments are wrapped in a second call object (`{"name": "functions.create_task", "arguments":
  {...}}`), or named with the `functions.` prefix, is unwrapped; founding's plan stage had ended with no tasks.
- `finish` on an evening brief, weekly review or monthly check that stored no brief is refused once with the
  `write_brief` call to make, then accepted as `partial`; `update_task` cannot close the task the agent is running.
- `assign_to` accepts an agent's name as well as its role, and agents cannot create tasks that duplicate a rhythm
  ("Write daily brief").
- From the second run: a brief may state the window in a metric's name ("30 days" for `stripe.revenue_usd_30d`); a
  brief task's deliverable is recorded as the brief ("the weekly memo (brief #3, on the Overview)") instead of the
  file path the model names, which the next planning cycle reported as missing; `update_task` queues a task the
  model sets to `in_progress` as `todo`, since only a starting run may mark it in progress.

## Scope

In this phase:

1. Company brain: `brain.py`, seven tables, a digest under 6,000 characters, read and write tools, the Brain tab.
2. Goals, bets and experiments, and the task contract fields on `create_task`.
3. Dependency graph in the scheduler and file leases in `workspace.py` and `tools.py`.
4. Rhythms: morning plan, evening brief, weekly review, monthly check, the `briefs` table, Telegram delivery.
5. Decisions queue: `approvals` rows of action `decision`, the `ask_decision` tool, deadlines.
6. Budgets: company settings and their enforcement points.

Not in this phase: the verification gate that runs `task.checks` (Phase 3, [phase-3-verify.md](phase-3-verify.md)),
the portfolio allocation score (Phase 4, [phase-4-scale.md](phase-4-scale.md)), model routing (Phase 3).

## Baseline: what exists today

"Today" below means the main branch as documented in the README, before Phase 0. Where Phase 0 or 1 changes
something this phase builds on, the phase is named.

| Area | Today | Phase 2 changes it to |
|---|---|---|
| Source of truth | `companies.charter` (Markdown, mirrored to `docs/charter.md`), truncated to 3,500 chars in `agents.system_prompt` | `brain.digest()` in the system prompt; the charter stays on disk and is listed under Files |
| Goals | KPIs as prose in the charter | `goals` rows with metric, target, current, deadline, owner |
| Metrics | `metrics(id, company_id, agent_id, name, value, note, created_at)`, typed in through `record_metric`; Phase 1 adds `source` | Shown in the digest with their source; the weekly review uses only rows whose source is a connector |
| Tasks | `tasks(... deliverable, acceptance, routine)`; Phase 0 adds `notes`, `reassigned` | Contract columns: `goal_id`, `expected`, `inputs`, `depends_on`, `due_at`, `budget_steps`, `budget_tokens`, `checks` |
| Dispatch | `Scheduler.tick` hands out every `todo` task by `priority, id` | Skips tasks whose dependencies are not done or cancelled |
| Parallel writes | Two agents can overwrite the same file | `leases` table; a write to a leased path returns an error |
| Rhythms | Preset routines (`presets.R`): CEO "Morning report" (24 h) and "Weekly review" (168 h), analyst "Weekly KPI report" (168 h), QA "Daily review" | Morning plan, evening brief, weekly review and monthly check driven by data that code gathers; `briefs` table |
| Owner questions | `approvals` action `question`; Phase 0 adds options and Telegram buttons | Action `decision` with options, a recommendation, a deadline and auto-resolution |
| Budgets | `daily_email_limit` (default 20) checked in `send_email` | GPU hours, steps per task, posts per week, money (display only) |

## Design

### 1. Company brain (`brain.py`)

Behaviour. The brain is the set of structured records every agent reads first. Agents read a compact digest at
the start of every run and look up the rest with `brain_lookup`. They change the brain only through tools, and
every change by a non-CEO agent is a proposal that the CEO reviews during planning. A proposal that changes
price, offer, positioning or ICP is routed to the owner as a decision instead of to the CEO.

Data. All tables go in `db.SCHEMA` as `CREATE TABLE IF NOT EXISTS`. Times are `db.now()` strings.

| Table | Columns | Notes |
|---|---|---|
| `strategy` | `company_id` PK, `one_liner`, `icp`, `problem`, `offer`, `price`, `positioning`, `voice`, `updated_at` | One row per company |
| `goals` | `id`, `company_id`, `title`, `metric`, `target` REAL, `current` REAL, `deadline`, `owner_role`, `status` (`active`, `done`, `dropped`), `created_at` | At most 3 `active` per company; `current` is refreshed from the latest `metrics` row named `metric` whenever the digest is built |
| `contacts` | `id`, `company_id`, `name`, `email`, `company`, `role`, `stage` (`lead`, `contacted`, `replied`, `customer`, `lost`), `source`, `consent`, `unsubscribed` INTEGER, `last_touch`, `notes`, `created_at` | Unique on (`company_id`, lower(`email`)) when email is set |
| `decisions` | `id`, `company_id`, `what`, `why`, `expected`, `review_at`, `outcome`, `decided_by`, `reversible` INTEGER DEFAULT 1, `created_at` | `decided_by` is the role key `ceo` (operating-model.md; the CEO is the only agent that logs decisions), `owner`, `telegram` or `deadline`; `reversible` stores `log_decision`'s argument and the decision payload's flag |
| `experiments` | `id`, `company_id`, `goal_id`, `hypothesis`, `metric`, `start`, `stop`, `result`, `verdict` (`pending`, `won`, `lost`, `inconclusive`), `created_at` | |
| `lessons` | `id`, `company_id`, `text`, `created_at` | Short rules; the digest shows the newest 8 |
| `brain_changes` | `id`, `company_id`, `agent_id`, `section`, `payload` JSON, `reason`, `status` (`pending`, `accepted`, `rejected`, `owner`), `decided_at`, `created_at` | `status` `owner` means it became a decision in the inbox |

`metrics.source` (Phase 1 migration; existing rows are `manual`) is what the digest and the weekly review read.

Digest. `brain.digest(company) -> str`, at most 6,000 characters (about 1,500 tokens). Sections in this order,
each trimmed to its own cap before the whole is cut at 6,000: strategy (7 lines), active goals with current vs
target and days left (3 lines), latest metric per name with its source (one line per metric, newest 20 names),
decisions due for review (`review_at <= today + 3 days`, outcome empty, up to 5), running experiments (up to 5),
lessons (newest 8), contact counts by stage, pending brain proposals (count and the first 3). Example, 1,900
characters:

```
# Company brain: PawPlan (digest, 2026-10-14)
## Strategy
One-liner: Weekly meal plans for dog owners, delivered as a printable PDF.
Customer (ICP): urban dog owners, 25-45, one dog, spending $60+/month on food.
Problem: owners guess portions and ingredients; vets' advice is generic.
Offer: a personalised weekly plan plus a shopping list. Price: $9/month.
Positioning: the plan your vet would write, at a fraction of the cost.
Voice: warm, practical, no hype.
## Goals (2 of 3 active)
#1 10 paying customers by 2026-12-01: stripe.customers 2 of 10, 48 days left, owner sales
#2 200 waitlist signups by 2026-11-15: forms.signups 61 of 200, 32 days left, owner marketing
## Metrics (latest per name; source in brackets)
stripe.revenue_usd_30d 18 [stripe] 2026-10-14
stripe.customers 2 [stripe] 2026-10-14
forms.signups 61 [forms] 2026-10-14
email.sent_7d 34 [email] 2026-10-14
email.replies_7d 3 [email] 2026-10-14
waitlist_signups 58 [manual, Tally] 2026-10-12
## Decisions due for review
#4 (review 2026-10-16) Price at $9/month, not $15. Expected: signup rate of 5% or more on the landing page. Decided by Atlas.
## Running experiments
#2 (goal #2, stops 2026-10-21) "A vet-endorsement line on the landing page lifts signup rate from 3% to 5%". Metric forms.signup_rate: 3.4 so far.
## Lessons (newest first)
- r/dogs removes posts with links; post the text and reply with the link.
- Cold emails over 80 words get no replies from pet shops.
## Contacts
41 leads, 12 contacted, 3 replied, 2 customers, 1 lost. 2 unsubscribed.
## Brain proposals waiting for CEO review: 1
#7 by Hunter (strategy.icp): add dog walkers to the ICP. Reason: 3 of 3 replies this week came from walkers.
```

The digest replaces the charter block in `agents.system_prompt`: the "# The company" part becomes the digest,
followed by "Charter: docs/charter.md (read it with read_file when you need the long form)". A company with an
empty brain (founded before this phase) gets the digest header plus "Brain not written yet; the CEO fills it in
the next planning cycle" and the old charter block, so nothing is lost. `cycle_prompt` tells the CEO to call
`set_strategy` and `set_goal` when the brain is empty.

Tools. Registered in `brain.py` with `@tools.tool(..., risk=...)`; `tools.py` imports `brain` at its bottom
(the Phase 0 convention). Arguments are strings unless noted. Every executor preset gets `brain_lookup`,
`propose_brain_change` and `add_lesson`; the five people-facing presets also get `add_contact` and
`update_contact`; the CEO gets `brain_lookup`, `add_lesson` and every CEO tool (it writes directly, so it has no
use for `propose_brain_change`).

| Tool | Arguments | Who | Risk | Behaviour |
|---|---|---|---|---|
| `brain_lookup` | `section` (`strategy`, `goals`, `contacts`, `decisions`, `experiments`, `lessons`, `proposals`), `query` (optional words) | every role | read | Returns up to 20 rows of that table matching `query` (LIKE over the text columns), newest first. Contacts never show `email` to roles other than sales, support, success, lifecycle, partnerships and the CEO |
| `propose_brain_change` | `section`, `change` (JSON object or plain text), `reason` | every role | write | Inserts a `brain_changes` row (`pending`). If `section` is `strategy` and `change` touches `price`, `offer`, `positioning` or `icp`, the row is marked `owner` and an `approvals` row of action `decision` is created (options: "Accept the change", "Reject it"; recommended "Reject it"; reversible True; deadline 72 h) |
| `add_contact` | `name`, `email`, `company`, `role`, `stage`, `source`, `consent`, `notes` | sales, support, success, lifecycle, partnerships | write | Inserts or, when the email exists, updates the contact. Returns the id |
| `update_contact` | `contact_id` (int), `stage`, `last_touch`, `consent`, `unsubscribed` (bool), `notes` | sales, support, success, lifecycle, partnerships | write | Partial update; `unsubscribed=true` is final (cannot be reset by an agent) |
| `add_lesson` | `text` | every role | write | Inserts a lesson (max 300 chars) |
| `set_strategy` | `one_liner`, `icp`, `problem`, `offer`, `price`, `positioning`, `voice` | CEO (and founding) | write | Upserts the `strategy` row. During founding every field is required |
| `set_goal` | `title`, `metric`, `target` (number), `deadline` (YYYY-MM-DD), `owner_role` | CEO (and founding) | write | Refuses a fourth active goal ("3 goals are active; drop or finish one first"). When the active set changes outside founding, a `decision` is sent to the owner: "Atlas changed the goals: ... Keep them?" options "Keep", "Revert"; recommended "Keep"; reversible; 48 h |
| `drop_goal` | `goal_id` (int), `status` (`done` or `dropped`), `note` | CEO | write | Closes a goal; tasks linked to it keep their `goal_id` and are listed in the next weekly review |
| `log_decision` | `what`, `why`, `expected`, `review_days` (int, default 14), `reversible` (bool) | CEO | write | Inserts a `decisions` row with `review_at = now + review_days`, `decided_by = "ceo"` and `reversible` as given. The digest marks irreversible decisions; a choice that needs the owner's say goes through `ask_decision` instead |
| `start_experiment` | `goal_id` (int), `hypothesis`, `metric`, `stop_days` (int, default 14) | CEO | write | Inserts an experiment with `start = today`, `stop = today + stop_days`, verdict `pending` |
| `conclude_experiment` | `experiment_id` (int), `result`, `verdict` | CEO | write | Sets result and verdict; verdict must be `won`, `lost` or `inconclusive` |
| `review_brain_change` | `change_id` (int), `decision` (`accept` or `reject`), `note` | CEO | write | Applies an accepted proposal to its table (strategy fields merged; a contact, lesson, decision or experiment inserted) and records the status. Proposals with status `owner` cannot be reviewed by the CEO |

`roles.FOUNDING_TOOLS` gains `set_strategy` and `set_goal`. Every executor preset gains `brain_lookup`,
`propose_brain_change` and `add_lesson`; the sales, support, success, lifecycle and partnerships presets also gain
`add_contact` and `update_contact` (the brief says every role; the narrower belt is recorded under Open
questions). `presets.CUSTOM_TOOLS` gains `brain_lookup` and `propose_brain_change`. Every preset drops `remember`:
lessons go through `add_lesson`, facts through `propose_brain_change`, and `recall` stays for reading the memory
table (owner notes and decision records still land there).

Belt sizes. Today's belts hold 9 to 14 tools and `TestPresets.test_integrity` caps them at 15; Phase 1 adds
`find_tool` (the CEO reaches 15). The brain tools do not fit under that cap: with them, sales reaches 17 and
engineering, ops, partnerships and success 16, and the CEO would reach 28. The new rule, written into
`test_integrity` with a comment that planning needs the brain tools: executor belts at most 18, the CEO's at most
24. The final CEO belt (23 tools) is `list_tasks`, `create_task`, `update_task`, `read_file`, `write_file`,
`web_search`, `recall`, `report_to_owner`, `ask_owner`, `update_charter`, `hire_agent`, `find_tool`,
`brain_lookup`, `add_lesson`, `set_strategy`, `set_goal`, `drop_goal`, `log_decision`, `start_experiment`,
`conclude_experiment`, `review_brain_change`, `ask_decision`, `finish`; it drops `remember` and `record_metric`
(connectors and the analyst feed the metrics table; the CEO reads it through the digest). `note_progress` is
added at run time by `roles.TASK_EXTRA` and `write_brief` is in the analyst and CEO belts, both counted.

Founding. `agents.founding_prompt` step 3 becomes: write the charter with `update_charter` (unchanged), then call
`set_strategy` with all seven fields, then `set_goal` one to three times (each a number with a date). Step 4
(first tasks) requires `goal_id` and `expected` on every task. The fake model's founding script is extended the
same way.

API routes (`brain.py`, registered with `@route`; these route names are chosen here, the brief does not fix them):

| Route | Purpose |
|---|---|
| `GET /api/companies/<id>/brain` | Every section as JSON plus `digest` and `digest_chars` |
| `POST /api/companies/<id>/brain/<section>` | Owner adds a row (`goals`, `contacts`, `decisions`, `experiments`, `lessons`) or sets `strategy` |
| `PATCH /api/brain/<section>/<row_id>` | Owner edits a row inline |
| `DELETE /api/brain/<section>/<row_id>` | Owner deletes a row (`strategy` cannot be deleted, only edited) |
| `POST /api/brain/changes/<id>` | Owner accepts or rejects a pending proposal (`{"decision": "accept" or "reject"}`) |

Owner edits emit `bus.emit(cid, "brain", "owner edited <section>")` so agents see the change in the next run.

UI. A new company tab `brain` (`TABS` gains `['brain', 'Brain']` after `tasks`). One panel per section: strategy
as a form with inline save; goals as a table (title, metric, current/target, deadline, owner, status) with an
"Add goal" row; contacts as a table with stage filter; decisions, experiments and lessons as lists with inline
edit; a "Proposals" panel with Accept and Reject buttons; a footer showing "Digest: N of 6,000 characters".
The Charter tab stays as it is.

Code touch points: `db.SCHEMA` (tables), `agents.system_prompt` (digest), `agents.founding_prompt`,
`agents.cycle_prompt` (section 2 below), `roles.FOUNDING_TOOLS`, `presets.py` (tool belts), `tools.py` (import
of `brain`), `static/app.js` (tab), `server.py` (nothing: routes live in `brain.py`).

### 2. Goals, bets and experiments; the task contract

Behaviour. Planning is top down: goal, then bets that could move it, then tasks. Each bet becomes an experiment
with a stop date. The CEO may not create a task without naming the goal it serves and the expected effect when
the company has active goals. A task created with no goal gets a warning in the tool result and is listed in the
weekly review for cancellation.

Data. `db.MIGRATIONS["tasks"]` gains:

| Column | Type | Set by | Meaning |
|---|---|---|---|
| `goal_id` | INTEGER | `create_task`, `update_task`, owner | The goal this task serves |
| `expected` | TEXT DEFAULT '' | `create_task` | What this task should move and by how much |
| `inputs` | TEXT DEFAULT '' | `create_task` | Files and facts the assignee must read first |
| `depends_on` | TEXT DEFAULT '[]' | `create_task` | JSON list of task ids that must be done or cancelled first |
| `due_at` | TEXT | `create_task` (`due_days`), owner | UTC deadline; empty means none |
| `budget_steps` | INTEGER | `create_task` | Tool calls allowed per attempt (capped by the company's `steps_per_task`) |
| `budget_tokens` | INTEGER | `create_task` | Tokens allowed across attempts; exceeded means the run ends with status `partial` and a note |
| `checks` | TEXT DEFAULT '' | `create_task` | One tool check per line, for example `browser_check site/index.html`; Phase 3's gate runs them |

`create_task` (tools.py) gains the arguments `goal_id` (int), `expected`, `inputs`, `depends_on` (comma-separated
task ids), `due_days` (int), `budget_steps` (int), `budget_tokens` (int), `checks`. Validation, all returning
`ERROR:` text the model can act on: `goal_id` must be an active goal of this company; a CEO-created task with no
`goal_id` while active goals exist returns "Created task #N, but it serves no goal. Add goal_id (active goals:
#1 ..., #2 ...) or it will be cancelled at the weekly review" (the task is still created); `depends_on` ids must
belong to this company and must not be `done` or `cancelled` already and must not create a cycle (checked by
walking the graph); `due_days` 1 to 90; `budget_steps` 3 to 60; `checks` lines must start with a known check
tool name (`browser_check`, `audit_page`, `review_text`, `check_contrast`, `fetch_url`). `update_task` gains
`goal_id` (int) so the CEO can attach a goal during the weekly review, and `due_days`. The owner's task form
(`POST /api/companies/<id>/tasks`, `PATCH /api/tasks/<id>`) accepts the same fields. `GET /api/companies/<id>/tasks`
returns them plus `blocked_by`: the ids in `depends_on` that are not yet done or cancelled.

`task_prompt` shows the contract above the details: "Goal: #1 10 paying customers by Dec 1 (this task should:
<expected>)", "Inputs: ...", "Due: ...", "Budget: N tool calls", "Checks that will run when you finish: ...".

`cycle_prompt` changes. The new prompt, in order:

```
Run a planning cycle for PawPlan.

## Goals (plan from these; at most 3 active)
#1 10 paying customers by 2026-12-01: stripe.customers 2 of 10, 48 days left, owner sales. Open tasks: 3.
#2 200 waitlist signups by 2026-11-15: forms.signups 61 of 200, 32 days left, owner marketing. Open tasks: 2.
(no goal) Open tasks: 1 (#31). Attach a goal with update_task(goal_id=...) or cancel them.

## Experiments
Running: #2 (goal #2, stops 2026-10-21) ... Metric forms.signup_rate: 3.4 so far.
Due to conclude: none. Call conclude_experiment for each one that is due.

## Brain proposals waiting for your review (review_brain_change)
#7 by Hunter (strategy.icp): add dog walkers to the ICP. Reason: ...

## Decisions due for review (log the outcome with log_decision, or propose a change)
#4 (review 2026-10-16) Price at $9/month, not $15. Expected: ...

## Open tasks (grouped by goal)
Goal #1: #28 [in_progress] (sales) p1 Email 20 pet shops -> sales/outreach-week3.md (due 2026-10-16)
...

## Recently finished (with automatic checks)
...

## Owner's recent decisions
...

## Recent chat with the owner
...

## Files in the company folder (newest first)
...

## Your job now
Plan top down. For each goal: what is the biggest gap between current and target, which 1-2 bets could close it,
and which tasks does each bet need. Write each bet as start_experiment(goal_id, hypothesis, metric, stop_days)
and give its tasks that goal_id and an expected effect. Verify the finished work above (reopen anything MISSING
or weak), conclude experiments that are due, review brain proposals, cancel stale tasks or tasks with no goal,
create at most 5 new tasks (each with goal_id, expected, deliverable, acceptance, and checks where a tool can
verify the work), handle anything the owner asked for, and report to the owner only if something matters. Then
call finish.
```

The sections that exist today (finished work, owner decisions, chat, files) keep their current content; the
"Recently finished" lines keep the `deliverable_status` check and, from Phase 3, the gate results.

Code touch points: `tools.create_task`, `tools.update_task`, `tools.list_tasks` (shows goal and due date),
`agents.task_prompt`, `agents.cycle_prompt`, `agents._task_lines` (grouping by goal), `server.api_task_create`,
`server.api_task_patch`, `server.api_tasks`, `static/app.js` `TabTasks` (new fields in both modals; goal select;
"waiting on #id" and "due" badges on cards).

### 3. Dependency graph and file leases

Dispatch rule. `Scheduler.tick` step 3 filters the `todo` list: a task is dispatchable only when every id in its
`depends_on` has status `done` or `cancelled`. The check is one query per tick: load the status of every id that
appears in any open task's `depends_on`. Tasks that are waiting show "waiting on #12" in the Tasks tab (from
`blocked_by`) and are not counted as idle work for `_maybe_cycle` (so a CEO cycle is not triggered just because
everything is waiting on one task). A task whose dependency is cancelled is dispatched with a prompt line
"Note: task #12 that this depended on was cancelled" so the assignee can decide. A dependency on a task that is
`blocked` keeps the dependent waiting; the watchdog pattern `stalled` (Phase 0) already covers a company where
nothing can run.

Leases. Table `leases(company_id, path, task_id, agent_id, run_id, expires_at)`, primary key (`company_id`,
`path`). `workspace.lease(company, path, ctx)` is called by `write_file` and `delete_file` before touching the
file:

- No lease, or a lease held by this `run_id`, or an expired lease: take or renew it with `expires_at = now + 30 min`
  and continue.
- A lease held by another live run: return `ERROR: site/index.html is being edited by Forge (task #12) until
  14:32 UTC; work on something else or wait.` The agent name and task come from the lease's `agent_id` and
  `task_id`. The write does not happen.

Leases are released in `agents._finalize` (`DELETE FROM leases WHERE run_id=?`), so a crashed run frees its files
at the latest 30 minutes later. `scheduler.recover()` clears every lease on start. Owner edits through
`PUT /api/companies/<id>/file` ignore leases (the owner is the board) but the Files tab shows an "editing: Forge"
badge next to a leased file (`GET /api/companies/<id>/files` returns `leases`). Leases cover exact paths only;
two agents writing different files in the same folder do not conflict.

Code touch points: `scheduler.Scheduler.tick`, `scheduler.recover`, `workspace.lease`, `workspace.release`,
`tools.write_file`, `tools.delete_file`, `agents._finalize`, `server.api_files`, `static/app.js` `TabTasks`
and `TabFiles`.

### 4. Rhythms (`rhythms.py` and preset routines)

Behaviour. The rhythms are recurring duties created by the scheduler exactly as today (`Scheduler._routines`
creates a task from `presets.R(...)`), with two additions: code gathers the data first and puts it in the task
prompt, and the agent stores its output with a tool instead of only writing a file. The model never invents a
number: the prompt says the data block is the only source of numbers for the brief, and the Bench (Phase 3)
checks that every number in a brief appears in its data.

`presets.R` gains an optional `at_hour` (local hour, 0-23). `Scheduler._routines` creates a routine task with
`at_hour` only once the local time has passed that hour and no task for that routine was created since the most
recent occurrence of that hour. This keeps "morning" and "evening" meaningful without a cron.

| Routine key | Who | Cadence | Data gathered by code | Model output | Stored as |
|---|---|---|---|---|---|
| `ceo:Morning plan` (replaces `ceo:Morning report`) | CEO | 24 h, `at_hour` 7, first after 20 h | `rhythms.morning_data`: goals with gap to target, open tasks per goal, blocked tasks, pending escalations and decisions, yesterday's brief | Top 3 priorities per goal appended to `docs/daily-plan.md` (newest on top), tasks queued with `create_task`, blockers raised with `ask_decision` or `ask_owner` | File plus tasks; no brief row |
| `analyst:Evening brief`, and `ceo:Evening brief` with `requires="no_analyst"` | Analyst, or the CEO when no enabled analyst exists | 24 h, `at_hour` 18, first after 20 h | `rhythms.daily_data` (below) | One-page brief written with `write_brief(kind="daily", body)` | `briefs` row, Telegram, Portfolio pin, company Overview |
| `ceo:Weekly review` (existing key, new content) | CEO | 168 h, `at_hour` 9, first after 160 h | `rhythms.weekly_data` (below) | Conclude experiments, log decision outcomes, attach goals or cancel goal-less tasks, adjust goals, then `write_brief(kind="weekly", body)` as the board memo | `briefs` row, Telegram |
| `ceo:Monthly strategy check` | CEO | 720 h, first after 700 h | `rhythms.monthly_data`: 4 weekly briefs' numbers, revenue trend, goals hit or missed, money spent, GPU hours | One `ask_decision`: "Keep, change or kill <company>?" options "Keep", "Change: <summary>", "Kill (pause the company)"; recommended by the CEO; `reversible=False` for the whole decision so it never resolves on its own; Kill sets the company to `paused` (Holdco never deletes a company); plus `write_brief(kind="monthly", body)` | `briefs` row, decision in the inbox |

`presets.R("Weekly KPI report")` for the analyst and `qa:Daily review` are unchanged. `scheduler._routines` gains
`requires="no_analyst"`: the routine is created only when no enabled agent with preset `analyst` exists.

`rhythms.daily_data(company) -> dict`, computed from the database only:

| Key | Content |
|---|---|
| `metrics` | For every metric name with a row in the last 24 h: latest value, value 24 h ago, value 7 days ago, deltas, `source` |
| `anomalies` | Metrics with a baseline (at least 3 earlier values) whose latest value moved more than 50% against the 7-day mean |
| `shipped` | Tasks set to `done` in the last 24 h: id, title, role, deliverable, `goal_id` |
| `stuck` | Tasks `blocked` or `waiting`, pending escalations (`approvals` action `question` with `payload.pattern`), connectors down (Phase 1 statuses) |
| `decisions_needed` | Pending `approvals` of action `decision` and `question`, with their recommendation and deadline |
| `goals` | Each active goal with current, target, days left |
| `approvals_pending` | Count by action |

`rhythms.weekly_data(company)` adds: goal progress this week vs last week from `metrics` rows whose `source` is
not `manual` (rows with `source = 'manual'` are listed separately under "self-reported, not used for scoring"),
experiments with `stop <= today` and verdict `pending`, decisions with `review_at <= today` and empty outcome,
open tasks with no `goal_id` that were not created by the owner and are not routines, tasks done this week per
goal, emails sent and replies (from the `emails` table), GPU hours and tokens used this week.

Prompting. `agents.task_prompt` appends, for a task whose `routine` key is in `rhythms.RHYTHM_KEYS`, a section
"## Data for this brief (the only numbers you may use)" containing the JSON above, pretty-printed and capped at
12,000 characters (oldest entries dropped first), and the instruction for that routine. The brief format the
prompt asks for: "What moved (numbers with their source), What shipped (with file paths), What is stuck, Decisions
needed (each with the recommendation), Tomorrow's focus", under 350 words.

Tool `write_brief(kind, body)` (every role that has a rhythm: analyst and CEO; risk write): validates `kind` in
`daily`, `weekly`, `monthly`; stores `briefs(id, company_id, kind, body, data JSON, created_at)` where `data` is
the dict that was in the prompt; emits `bus.emit(cid, "brief", ...)`; calls `notify.send(text, kind="brief")` with
the body converted to Telegram HTML (headings bold, lists as lines), truncated to 3,500 characters with a link
to the dashboard. The brief row is the deliverable: `finish` passes the deliverable check when a brief of that
kind was written in this run (`ctx.brief_written`), so no file is required.

Portfolio brief. `rhythms.portfolio_brief()` runs from `Scheduler._housekeeping` once a day: when every active
company has a daily brief less than 20 h old, or 2 h after the first company brief of the day, it builds a
portfolio brief in code (no model): per company the goal lines, the top 3 metric deltas, shipped count, stuck
count and decisions needed, then "Decisions waiting for you: N". Stored as `briefs` with `company_id NULL`,
`kind='daily'`, sent to Telegram once, and pinned at the top of the Portfolio page. The company Overview shows
the company's latest daily brief and a "Weekly memo" link.

Weekly housekeeping. After the CEO's weekly review run finishes (`agents._finalize`, task routine key
`ceo:Weekly review`), `rhythms.after_weekly(company)` cancels every open task that still has no `goal_id`, was
created by an agent and is not a routine, with the note "[Weekly review] Cancelled: serves no goal". Tasks the
owner created are never cancelled automatically; they are listed in the memo under "Owner tasks without a goal".

API routes (chosen here): `GET /api/briefs?company_id=&kind=&limit=` (company_id 0 for portfolio),
`GET /api/briefs/latest?company_id=`, `POST /api/companies/<id>/rhythms/<key>/run` where key is `morning`,
`evening`, `weekly` or `monthly` (creates the routine task now; "Run now" buttons on the Overview).

Code touch points: `presets.R`, `presets.PRESETS["ceo"]["routines"]`, `presets.PRESETS["analyst"]["routines"]`,
`scheduler._routines`, `scheduler._housekeeping`, `agents.task_prompt`, `agents._finalize`, `tools.finish`
(brief as deliverable), `notify.send` (kind `brief`, Phase 0 already lists "daily brief (Phase 2)" as a hook),
`static/app.js` `Portfolio` (pinned brief) and `TabOverview` (latest brief, Run now).

### 5. Decisions queue

Behaviour. Approvals are "may I send this?". Decisions are "which of these should we do?": options, a
recommendation and a deadline after which the agent's recommended option is taken, if the decision is reversible.
Irreversible decisions wait for the owner.

Data. `approvals` rows with `action = 'decision'` and payload:

| Key | Type | Meaning |
|---|---|---|
| `question` | str | The question, one paragraph |
| `options` | list[str] | 2 to 5 options |
| `recommended` | str | One of `options` |
| `deadline_at` | UTC string | When the scheduler may auto-resolve |
| `reversible` | bool | False means never auto-resolved |
| `context` | str | Why now, what was tried (up to 1,500 chars) |

`db.MIGRATIONS["approvals"]` gains `decided_by TEXT DEFAULT ''` ("owner", "deadline", or "telegram"). `question`
rows from Phase 0 keep their shape; both actions share the Telegram callback form `q:<approval_id>:<option index>`.

Tool `ask_decision(question, options, recommended, deadline_hours=48, reversible=True)` for the CEO (risk write;
`options` is one option per line; `recommended` must equal one option; `deadline_hours` 1 to 336). It calls
`actions.gate(..., "decision", payload)`; `gate` treats `decision` like `question` (always queued, never auto,
deduplicated on the question text). The tool result: "Decision #N sent to the owner. If it is reversible and
unanswered by <deadline>, '<recommended>' will be taken. Continue with work that does not depend on it, or
finish with status 'waiting'." `ask_owner` keeps working for simple questions.

Resolution. `actions.decide(approval_id, "approve", answer=<option>)` with action `decision` behaves like a
question answer: status `answered`, `result` = the option, `decided_by` = "owner" (or "telegram" when the Phase 0
poller calls it), a memory note, the waiting task reopened with "[Update] Owner decided #N: <option>", and
`companies.cycle_requested = 1`. `decide` on a `decision` row also inserts a `decisions` row (`what` = the chosen
option, `why` = the question plus context, `expected` from the payload when given, `review_at` = now + 14 days,
`decided_by` = `owner`, `telegram` or `deadline`, `reversible` from the payload), so the brain remembers what
the owner chose and the weekly review brings it back for an outcome. `actions.resolve_due_decisions()` runs every scheduler tick (from
`_housekeeping`): for every pending `decision` with `reversible` true and `deadline_at <= now`, it calls
`decide(..., answer=recommended)` with `decided_by = "deadline"`, and `notify.send("Decision #N was taken by the
deadline: <recommended>")`. Irreversible decisions past their deadline are left pending and re-notified once a
day. The kill switch (Phase 0) blocks auto-resolution too.

UI. The Inbox gets a "Decisions" panel above "Requests": each card shows the question, the context folded, one
button per option with the recommended one styled `primary` and labelled "(recommended)", the deadline as "auto
in 31 h" or "waits for you", and a free-text note. Telegram messages show one inline button per option (Phase 0
renderer). `GET /api/approvals` returns decisions with the rest; `app.js` splits by `action`. The sidebar count
includes them.

Code touch points: `actions.gate` (decision like question), `actions.decide` (`decided_by`, answer for
decisions), `actions.resolve_due_decisions`, `actions.ACTION_LABELS["decision"] = "Decision"`,
`scheduler._housekeeping`, `notify.py` (text and buttons for decisions), `tools.ask_decision`,
`presets.PRESETS["ceo"]["tools"]`, `static/app.js` `Inbox`, `requestFields`, `ACTION_VERB`.

### 6. Budgets

Settings. `config.DEFAULT_COMPANY_SETTINGS` gains:

| Setting | Default | Enforced where |
|---|---|---|
| `gpu_hours_per_day` | 6 | `Scheduler.tick`: a company whose runs started today sum to more than this (run durations from `runs.started_at` and `finished_at`, running runs counted to now) gets no task dispatch and no CEO cycle until the next UTC day; its routine tasks are still created and queue. The Overview shows "GPU budget used: 4.2 of 6 h" |
| `steps_per_task` | 0 (meaning `max_steps_per_run`) | `agents.run` for kind `task`: `max_steps = min(steps_per_task or max_steps_per_run, task.budget_steps or 999)` |
| `daily_email_limit` | 20 (exists) | `send_email` as today; shown in the Budgets section as "Emails per day" |
| `posts_per_week` | 10 | `social_post`: counts `approvals` of action `social_post` with status `done` plus pending in the last 7 days; at the limit the tool returns "Weekly post budget (10) reached" |
| `money_usd` | 0 | Display only: the Budgets section shows "Money: $0 budget; $X approved this month" from `spend_money` approvals with status `done`. Purchases stay locked (`config.LOCKED_POLICIES`) |

`budget_tokens` on a task: `agents.run` adds the run's tokens to `tasks.tokens_used` (new column, migration) at
the end; when a task's `tokens_used` already exceeds `budget_tokens` at the start of an attempt, the task is set
to `blocked` with the note "Token budget exhausted" and the watchdog pattern `task_failed_twice` options apply.

UI. Company Settings tab gets a "Budgets" section with the five fields. `PATCH /api/companies/<id>` accepts them
(GPU hours as a number of at least 0.25 so a short trial is possible; steps, posts, emails and money as whole
numbers of at least 0, where 0 steps means the app default).

Code touch points: `config.DEFAULT_COMPANY_SETTINGS`, `server.api_company_patch` (integer coercion list),
`scheduler.Scheduler.tick` and `_maybe_cycle`, `agents.run`, `tools.social_post`, `static/app.js` `TabSettings`
and `TabOverview`.

## Files

New: `holdco/brain.py`, `holdco/contracts.py`, `holdco/rhythms.py`, `holdco/decisions.py`, `tests/test_phase2_brain.py`,
`tests/test_phase2_contracts.py`, `tests/test_phase2_rhythms.py`.

Changed: `holdco/db.py` (SCHEMA: `strategy`, `goals`, `contacts`, `decisions`, `experiments`, `lessons`,
`brain_changes`, `leases`, `briefs`; MIGRATIONS: tasks `goal_id`, `expected`, `inputs`, `depends_on`, `due_at`,
`budget_steps`, `budget_tokens`, `checks`, `tokens_used`; approvals `decided_by`), `holdco/config.py` (budget
settings), `holdco/tools.py` (`create_task`, `update_task`, `list_tasks`, `write_file`, `delete_file`,
`social_post`, `finish`, `ask_decision`, `write_brief`, import of `brain`), `holdco/agents.py` (`system_prompt`,
`cycle_prompt`, `task_prompt`, `founding_prompt`, `run`, `_finalize`), `holdco/actions.py` (`gate`, `decide`,
`resolve_due_decisions`, labels), `holdco/scheduler.py` (dependencies, GPU budget, `at_hour`, `no_analyst`,
`resolve_due_decisions`, `portfolio_brief`, `recover`), `holdco/workspace.py` (`lease`, `release`),
`holdco/roles.py` (`FOUNDING_TOOLS`), `holdco/presets.py` (belts, routines, `R(at_hour=)`), `holdco/notify.py`
(brief and decision rendering), `holdco/server.py` (task fields, files leases), `holdco/static/app.js` (Brain
tab, Tasks tab fields, Inbox decisions, Portfolio pin, Overview brief, Budgets), `holdco/static/style.css`
(decision card, brain tables), `tests/fake_ollama.py` (founding script with `set_strategy` and `set_goal`,
cycle script with `goal_id`, "Evening brief" and "Weekly review" scripts that call `write_brief`), `README.md`
(Brain tab, decisions, budgets).

## Tests

Unit (`tests/test_phase2_brain.py`, `tests/test_phase2_contracts.py` and `tests/test_phase2_rhythms.py`, one per
area, using `make_company` and `ctx_for` from `test_unit.py`):

| Test | Proves |
|---|---|
| `test_digest_under_limit` | A company seeded with 40 metrics, 30 lessons, 200 contacts produces a digest of at most 6,000 characters with every section header present |
| `test_digest_replaces_charter` | `agents.system_prompt` contains "# Company brain" and not the charter body; an empty-brain company still shows the charter |
| `test_brain_tools_by_role` | Sales can `add_contact`; marketing cannot (`no tool called 'add_contact'`); only the CEO can `set_goal`; the CEO belt is exactly the 23 tools listed in item 1 and `test_integrity` holds every executor at 18 or fewer |
| `test_goal_limit_and_owner_decision` | A fourth `set_goal` is refused; a goal change after founding creates a `decision` row with options Keep and Revert |
| `test_proposal_routing` | `propose_brain_change(section="strategy", change='{"price": "$15"}')` creates a `decision`, not a pending CEO review; a lesson proposal is `pending`; `review_brain_change` applies it |
| `test_create_task_contract` | `goal_id` validation, the no-goal warning text, `depends_on` cycle refusal, `checks` line validation, `due_days` to `due_at` |
| `test_dependency_dispatch` | `Scheduler.tick` (called directly with the fake model) does not start a task whose dependency is `todo`, starts it once the dependency is `cancelled` |
| `test_file_lease_conflict` | Two `Ctx` with different `run_id`: the second `write_file` to the same path returns the exact `ERROR:` text; after `workspace.release(run_id)` it succeeds; an expired lease is taken over |
| `test_daily_data_from_connector_metrics` | With metrics rows of source `stripe` and `manual`, `rhythms.daily_data` reports deltas and an anomaly; `weekly_data` lists manual rows under self-reported |
| `test_write_brief_and_notify` | `write_brief` stores the row with `data`, emits a `brief` event and calls `notify.send` once (fake Telegram from Phase 0) |
| `test_after_weekly_cancels_goalless` | Agent-created tasks without a goal are cancelled with the note; owner-created and routine tasks survive |
| `test_decision_deadline` | A reversible decision past its deadline is resolved with the recommended option, `decided_by = "deadline"`, the waiting task reopened, one notification sent, and a `decisions` row with `decided_by = "deadline"` and `reversible = 1` written; an owner answer writes one with `decided_by = "owner"`; an irreversible one stays pending |
| `test_budgets` | `social_post` refuses at `posts_per_week`; `agents.run` uses `min(steps_per_task, budget_steps)`; a company over `gpu_hours_per_day` gets no dispatch but still gets routine tasks |

End to end (`tests/test_e2e.py`, new tests numbered after Phase 1's `test_29`):
`test_30_founding_writes_brain` (founding with the fake model writes a strategy row and two goals, and every
founding task has a `goal_id`); `test_31_rhythms_and_briefs` (forcing the routines, by setting
`companies.created_at` back and the fake clock past `at_hour`, creates "Evening brief" and "Weekly review"
tasks; after they run, `GET /api/briefs/latest?company_id=` returns a daily and a weekly brief whose
`data.metrics` entries carry `source: "stripe"`, rows injected through the Phase 1 fake Stripe metrics poll);
`test_32_decision_in_inbox` (the Inbox endpoint lists a `decision` row created by the fake CEO's `ask_decision`,
and the dashboard Brain tab assets load); `test_33_dependency_and_lease` (a task that depends on an
open task is not dispatched until that task is done, and the task list shows `blocked_by`).

Fakes: `tests/fake_ollama.py` gains the scripts above. No new fake services; the Phase 0 fake Telegram and the
Phase 1 fake Stripe are reused.

## Gate and how the suite proves it

Gate: every open task links to a goal (the weekly review cancels the rest), and the weekly review's numbers come
from `metrics` rows with a connector source, never from agents' summaries.

Proof in the suite: `test_after_weekly_cancels_goalless` plus the e2e check that after the fake weekly review
`SELECT COUNT(*) FROM tasks WHERE status IN ('todo','in_progress','blocked','waiting') AND goal_id IS NULL AND
created_by != 'owner' AND routine = ''` is 0; `test_daily_data_from_connector_metrics` and the e2e brief check
assert that every number under `data.goals[*].current` and `data.metrics` in the weekly brief comes from a row
with `source != 'manual'`, and that manual rows appear only under `self_reported`. On your PC the gate is met
when the Tasks tab shows no open task without a goal badge after a weekly review and the board memo's numbers
match the Stripe test dashboard.

## How to try it on your PC

1. Check out `claude/phase-2-organize`, start Holdco, and open an existing company: the Overview shows "Brain not
   written yet" until the next planning cycle; press "Plan now" and watch the CEO call `set_strategy` and
   `set_goal` in the Thinking tab.
2. Open the Brain tab. Edit the price inline; then ask the sales agent in Chat to "propose adding dog walkers to
   our customer profile" and see the proposal appear under Proposals (CEO review) while a price proposal from an
   agent lands in your Inbox as a decision.
3. On the Tasks tab, add a task with a goal, a dependency on another open task and `browser_check site/index.html`
   under checks. It shows "waiting on #N" until that task is done.
4. Overview: press "Run now" next to Evening brief. The brief appears on the Overview, on the Portfolio page and
   on your phone (Phase 0 Telegram). Check that every number in it is in the Brain or Numbers panel.
5. Company Settings: set GPU hours per day to a small value and watch the Overview show the budget filling; agents
   stop picking up tasks once it is used.
6. Inbox: answer a decision from the Telegram buttons; the Decisions panel updates and the waiting task reopens.

## Open questions

1. Routine tasks (`created_by = 'routine'`) and owner-created tasks are exempt from the goal rule and from
   automatic cancellation. The gate text says "every open task"; this spec reads it as every task an agent
   created. Confirm.
2. The `at_hour` scheduling uses the PC's local time zone. An owner running Holdco on a server in another zone
   gets briefs at the server's evening; a `timezone` setting is not in the brief.
3. Contact emails are hidden from roles that do not message people. The brief does not say this; it is a
   privacy choice that keeps personal data out of prompts that do not need it.
4. Portfolio brief timing ("2 h after the first company brief") is a choice made here; the brief only says the
   daily brief is sent to Telegram and pinned on the Portfolio page.
5. Deviation: the brief gives `add_contact` and `update_contact` to every role; this spec gives them to the five
   people-facing presets (sales, support, success, lifecycle, partnerships). Reasons: executor belts would
   otherwise exceed the 18-tool cap, and personal data stays out of prompts that do not need it (the same reason
   `brain_lookup` hides emails from other roles). operating-model.md's Tools table says the same.
