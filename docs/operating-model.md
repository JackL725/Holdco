# Operating model

Holdco runs each company as a loop: goals set the work, tools verify it, connectors measure it, and the weekly
review turns results into the next plan. This document describes the pieces of that loop as they will exist in
the code: the company brain (one structured record every agent reads first), goals and experiments, task
contracts with dependencies and file leases, the daily, weekly and monthly rhythms, the verification gate,
budgets, the watchdog and the decisions queue. For each piece it states what the code does today and what each
phase adds. The owner's side of the loop (briefs, decisions, the Doctor page, the phone) is in
owner-experience.md; the module map is in architecture.md; the connectors that feed the metrics are in
connectors.md.

Status: design; built in Phase 2 (see phases/phase-2-organize.md). It depends on the watchdog, escalations,
working notes and attempt limit from Phase 0 (phases/phase-0-stabilize.md), on the `metrics.source` column and
connector health from Phase 1 (phases/phase-1-connect.md), and on the verification gate and trust ladder from
Phase 3 (phases/phase-3-verify.md).

## The ten rules

The plan (docs/plan.md, "How I would run a company with 20 copies of myself") sets ten rules. Each maps to a
mechanism that holds even when the model would not follow the rule on its own.

| # | Rule | Mechanism in Holdco | Phase |
|---|---|---|---|
| 1 | One written source of truth | The brain tables (`strategy`, `goals`, `contacts`, `decisions`, `experiments`, `lessons`) and `brain.digest()` at the top of every system prompt. Agents change the brain only through `propose_brain_change`, reviewed by the CEO; price, offer, positioning and ICP changes go to the owner. | 2 |
| 2 | Few goals, all measurable | `set_goal` refuses a fourth active goal. A goal is a metric name, a numeric target and a deadline. The CEO's `create_task` must name a `goal_id` while the company has active goals. | 2 |
| 3 | One owner per outcome, not per activity | `goals.owner_role` names one role per goal. Every task has one `agent_id`, one `deliverable` and a contract (`inputs`, `expected`, `checks`, `due_at`). Today's tasks already have one assignee, a deliverable and acceptance criteria. | today (one assignee), 2 (contract) |
| 4 | Trust tools, not each other | `finish(status=done)` runs the deliverable check today and the verification gate in Phase 3 (`verify.py`). The CEO's review prompt gets the gate results, and the QA agent reviews only what tools cannot check. | today (deliverable check), 3 (gate) |
| 5 | Ship small, measure, decide | Every bet becomes an `experiments` row with a stop date. The weekly review asks for a verdict on every experiment past its stop date, with the metric values beside it. | 2 |
| 6 | Talk to reality early | Metrics carry a `source` column. The weekly review and the daily brief are built from rows whose source is a connector, never from agents' summaries. Research deliverables must cite URLs that were fetched (Phase 3 gate). | 1 (source), 2 (review), 3 (research gate) |
| 7 | Write down every decision and lesson | `log_decision` and `add_lesson` write the `decisions` and `lessons` tables. Decisions carry a `review_at` date and come back in the weekly review. Today, `remember(kind=decision)` and `remember(kind=lesson)` write the memory table. | today (memory), 2 (tables) |
| 8 | Budgets on everything | Company settings `gpu_hours_per_day`, `steps_per_task`, `emails_per_day` (today's `daily_email_limit`), `posts_per_week`, `money_usd` (always 0), plus per-task `budget_steps` and the global `max_task_attempts`. Each has one enforcement point in code (see Budgets and watchdog). | today (emails, money lock), 0 (attempts), 2 (the rest) |
| 9 | Escalate early, in one place, with a recommendation | `watchdog.tick()` turns stuck patterns into one `question` row in the inbox with `options` and a `recommended` answer. `ask_decision` does the same for the CEO's own choices. Both reach the phone as buttons. | 0 (watchdog), 2 (decisions) |
| 10 | The owner is the board, not the bottleneck | The daily brief and weekly board memo (`briefs` table), reversible decisions that resolve themselves at a deadline, and the trust ladder that lets proven low-risk actions run alone. | 2 (briefs, decisions), 3 (trust) |

## Company brain

### Today

The company's shared knowledge lives in three places, and agents rebuild context from them on every run
(`agents.system_prompt`):

| Store | Where | What the prompt shows |
|---|---|---|
| Charter | `companies.charter`, mirrored to `docs/charter.md` by `update_charter` | The first 3,500 characters, then a "[charter truncated]" marker |
| Memory | `memory` table (`kind` is note, fact, decision, lesson or contact; `pinned` flag) | Up to 15 pinned entries (400 chars each) and the 14 newest unpinned (300 chars each) |
| Metrics | `metrics(id, company_id, agent_id, name, value, note, created_at)`, written by `record_metric` | The latest value per metric name with its date |

The prompt also carries `companies.mission` (the one-liner), `stage`, the owner's standing orders
(`directives`), the team list and the count of pending approvals. `recall(query)` searches memory with FTS5 when
SQLite has it, else with LIKE. Nothing in this layer is structured: goals are prose in the charter, customers are
prose in files under `sales/`, and a decision is a line in `docs/decisions.md` if the CEO remembers to write it.

### Planned (Phase 2): the tables

`brain.py` owns these tables. All are created in `db.SCHEMA` with `CREATE TABLE IF NOT EXISTS`; times are UTC
strings from `db.now()`.

**strategy** (one row per company)

| Column | Meaning |
|---|---|
| company_id | Primary key |
| one_liner | What we do and for whom (also written to `companies.mission`) |
| icp | The customer: who buys, in one or two sentences |
| problem | The problem they pay to solve |
| offer | What we sell |
| price | The price or price list, as text |
| positioning | Why us and not the alternatives |
| voice | Brand voice rules every writer follows |
| updated_at | Last change |

**goals**

| Column | Meaning |
|---|---|
| id, company_id | |
| title | Plain-language goal, for example "10 paying customers by Dec 1" |
| metric | The metric name that measures it, for example `stripe.customers` |
| target | REAL. The number to reach |
| current | REAL. Latest value of `metric`, refreshed by the rhythms from the metrics table |
| deadline | Date by which `target` must be met |
| owner_role | The one role that owns the outcome |
| status | active, done or dropped |
| created_at | |

**contacts** (the small CRM)

| Column | Meaning |
|---|---|
| id, company_id | |
| name, email, company, role | Who they are |
| stage | lead, contacted, replied, customer or lost |
| source | Where the contact came from: a form, a reply, research, a connector |
| consent | 1 when they agreed to be contacted (EU contacts need this before outreach; see safety.md) |
| unsubscribed | 1 once they opt out; outreach to them is refused |
| last_touch | When we last contacted them |
| notes | Free text |
| created_at | |

**decisions**

| Column | Meaning |
|---|---|
| id, company_id | |
| what | The decision |
| why | The reasoning |
| expected | The expected result, as something measurable |
| review_at | When to check whether it worked |
| outcome | Filled in at review time |
| decided_by | ceo, owner or deadline |
| reversible | 1 when the decision can be undone. Not in the brief's column list; added so `log_decision(reversible)` has somewhere to store its argument |
| created_at | |

**experiments**

| Column | Meaning |
|---|---|
| id, company_id, goal_id | The goal this bet serves |
| hypothesis | "At least X% of Y will do Z within N days" |
| metric | The metric name that decides it |
| start, stop | Start date and stop date (set from `stop_days`) |
| result | What happened, as numbers |
| verdict | pending, won, lost or inconclusive |
| created_at | |

**lessons**: `id, company_id, text, created_at`. Short rules learned the hard way.

**brain_changes**

| Column | Meaning |
|---|---|
| id, company_id, agent_id | Who proposed it |
| section | Which table: strategy, goals, contacts, decisions, experiments or lessons |
| payload | JSON: the proposed change (fields and new values), or `{"text": ...}` when the agent sent prose |
| reason | Why |
| status | pending, accepted, rejected or owner (routed to the owner as a decision) |
| decided_at, created_at | |

**metrics** gains `source` in Phase 1 (migration; existing rows become `manual`). A connector writes rows named
`<connector>.<metric>` with its own key as source; `record_metric` writes `manual`.

### The digest

`brain.digest(company)` returns at most 6,000 characters (about 1,500 tokens) and replaces the charter section
in `system_prompt`. The charter stays in `docs/charter.md` and appears in the "Files" list like any other file.
The digest holds, in this order:

1. Strategy, one line per field.
2. Active goals: title, `current` vs `target`, days left, owner role.
3. The latest value of every metric name, with its source and date.
4. Open decisions whose `review_at` has passed or falls within the next 7 days.
5. Running experiments (verdict pending): hypothesis, metric, stop date.
6. The newest 8 lessons.
7. Contact counts by stage.

When the content exceeds the limit, sections are trimmed from the bottom up (contacts, lessons, experiments)
so strategy and goals always fit whole.

### Reading and changing the brain

Every role reads the digest in its system prompt and looks up the rest with `brain_lookup(section, query)`,
which searches one table and returns the matching rows as text.

Agents change the brain through `propose_brain_change(section, change, reason)`, which inserts a
`brain_changes` row with status `pending`. The CEO reviews pending proposals during the planning cycle
(`cycle_prompt` lists them) with `review_brain_change(id, accept|reject, note)`. An accepted proposal is applied
to its table and the row is marked `accepted` with `decided_at`. A proposal that changes `strategy.price`,
`offer`, `positioning` or `icp` is never decided by the CEO: `propose_brain_change` sets its status to `owner`
and files a decision in the inbox (see Decisions vs approvals); the owner's answer applies or rejects it.

Two kinds of write bypass the proposal queue because they are cheap and reversible. `add_lesson` writes
directly for every role; `add_contact` and `update_contact` write directly for the roles that message people
(sales, support, success, lifecycle, partnerships). The brief gives the two contact tools to every role; the
narrower belt is a recorded deviation (phases/phase-2-organize.md, Open questions) that keeps executor belts
under the size cap and personal data out of prompts that do not need it. The CEO's own tools (`set_strategy`,
`set_goal`, `drop_goal`, `log_decision`, `start_experiment`, `conclude_experiment`) write directly, since the
CEO is the reviewer; `set_goal` still tells the owner through a decision whenever the goal set changes.

Founding writes the brain. The founding prompt tells the CEO to call `set_strategy` from the charter it just
wrote and `set_goal` for the first goals (one to three). `update_charter` stays for the long-form document.

The owner edits any section inline from the company's Brain tab (`#/c/<id>/brain`). An owner edit writes the
table directly; the next run sees it in the digest.

### Tools

| Tool | Arguments | Who | Risk tag (Phase 1) | What it does |
|---|---|---|---|---|
| brain_lookup | section, query | every role | read | Search one brain table; returns matching rows as text |
| propose_brain_change | section, change (JSON or text), reason | every role | write | Insert a pending `brain_changes` row; the result names its id and whether it went to the CEO or the owner |
| add_contact | name, email, company, role, stage, source, consent, notes | sales, support, success, lifecycle, partnerships | write | Insert a contact; an existing email in the same company updates that row instead |
| update_contact | contact_id, stage, last_touch, consent, unsubscribed | sales, support, success, lifecycle, partnerships | write | Update one contact's stage and flags |
| add_lesson | text | every role | write | Insert a lesson |
| set_strategy | one_liner, icp, problem, offer, price, positioning, voice | CEO (founding and cycles) | write | Write the strategy row; `one_liner` also updates `companies.mission` |
| set_goal | title, metric, target, deadline, owner_role | CEO | write | Insert an active goal; ERROR when 3 are already active; files a decision telling the owner the goal set changed |
| drop_goal | goal_id, status (done or dropped), note | CEO | write | Close a goal as done or dropped; its tasks keep their `goal_id` and are listed in the next weekly review |
| log_decision | what, why, expected, review_days, reversible | CEO | write | Insert a decision with `review_at` = now + review_days, `decided_by` = ceo and `reversible` as given |
| start_experiment | goal_id, hypothesis, metric, stop_days | CEO | write | Insert an experiment with `stop` = now + stop_days |
| conclude_experiment | id, result, verdict | CEO | write | Set result and verdict (won, lost or inconclusive) |
| review_brain_change | id, accept or reject, note | CEO | write | Decide a pending proposal and apply it when accepted |
| ask_decision | question, options, recommended, deadline_hours=48, reversible=true | CEO | write | File a decision for the owner (see Decisions vs approvals) |
| write_brief | kind (daily, weekly, monthly), body | analyst, CEO | write | Store the `briefs` row with the data block the prompt carried, emit a `brief` event and send the body to Telegram; the row counts as the routine task's deliverable |

"Who" means the tool belt in `presets.py`. Belts stay small (9 to 14 tools per role today; Phase 2 raises the
preset cap to 18 for executors and 24 for the CEO, see phases/phase-2-organize.md), so a role gets only the
brain tools its playbook uses; `brain_lookup`, `propose_brain_change` and `add_lesson` go into every executor
preset, and the CEO gets `brain_lookup` and its own write tools (it writes directly instead of proposing). Risk
tags and the policies they map to are in safety.md.

## Goals, bets and experiments

Planning is top-down. In Phase 2 `cycle_prompt` opens with the active goals (current vs target, days left) and
asks the CEO to work in this order: for each goal, name the bets that could move it; turn each bet into an
experiment with `start_experiment` (hypothesis, metric, stop date); then create the tasks that run the
experiment. Today's `cycle_prompt` opens with the open task list and asks for at most 3 priorities; it has no
goals to point at.

### The create_task contract

`create_task` keeps today's arguments (`title`, `assign_to`, `description`, `deliverable`, `acceptance`,
`priority`) and gains these:

| Argument | Column | Meaning | Rule |
|---|---|---|---|
| goal_id | tasks.goal_id | The goal this task serves | Required when the caller is the CEO and the company has active goals. Any other caller may omit it; the task is created, the tool result carries a warning, and the task is listed for cancellation in the weekly review |
| expected | tasks.expected | "What this task should move and by how much" | Free text; shown in the task prompt and the weekly review |
| inputs | tasks.inputs | Files or brain sections the assignee should read first | Free text; shown in the task prompt |
| depends_on | tasks.depends_on | Task ids that must finish first | JSON list of ids in the same company; an unknown id is an ERROR |
| due_days | tasks.due_at | Days until the task is due | Stored as a UTC time (now + due_days); used by the Phase 3 dispatch priority (due within 24 h) and shown in the Tasks tab |
| budget_steps | tasks.budget_steps | Tool calls allowed per run of this task | A run gets min(`steps_per_task`, `budget_steps`) |
| budget_tokens | tasks.budget_tokens | Tokens the task should not exceed | Advisory in Phase 2: stored and shown, not enforced (steps are the enforced limit) |
| checks | tasks.checks | Tool checks, one per line, for example `browser_check site/index.html` | Run by the Phase 3 gate when the task finishes; shown in the task prompt under the acceptance criteria |

### The weekly cancellation rule

During the weekly review (see Rhythms), code lists every open task (`todo`, `in_progress`, `blocked`,
`waiting`) whose `goal_id` is empty or points to a goal that is not active. The review prompt asks the CEO to
attach each one to a goal with `update_task(goal_id=...)` or leave it. Whatever is still unattached when the
review run ends is cancelled by code with the note "Cancelled by the weekly review: serves no active goal".
Two kinds are exempt: routine tasks (`tasks.routine` set; the scheduler creates them from presets, which know
no goals) and tasks the owner created (`created_by` = owner). The Phase 2 gate is this rule: after a weekly
review, every open non-exempt task links to a goal.

### Experiments and verdicts

An experiment is a bet with a stop date. `start_experiment` creates it, always under a goal. The weekly review
lists every experiment whose `stop` has passed and whose verdict is `pending` ("verdicts due"), with the
metric's value at `start` and now, both from the metrics table. The CEO calls
`conclude_experiment(id, result, verdict)`:

| Verdict | Meaning | What follows |
|---|---|---|
| won | The hypothesis held at the threshold it stated | The CEO logs a decision to keep or scale the change |
| lost | It did not | The CEO logs a lesson and a decision to stop |
| inconclusive | Not enough data by the stop date | Either a new experiment with a later stop, or drop it |

Verdicts and results go into the board memo. Code never concludes an experiment: the verdict is a judgement,
but the prompt gives the CEO only metric rows to judge from.

## Task contracts and the dependency graph

### Every task field

| Field | Since | Meaning |
|---|---|---|
| id, company_id | today | |
| title | today | Short imperative title; a duplicate among open tasks is refused |
| description | today | Objective, inputs and out-of-scope |
| role, agent_id | today | The assignee's role and agent; the scheduler refills `agent_id` from the role when the assignee is disabled |
| status | today | todo, in_progress, done, blocked, waiting or cancelled |
| priority | today | 1 urgent, 2 normal, 3 later |
| created_by | today | An agent name, `owner` or `routine` |
| result | today | The running log of handoffs and notes (newest 6,000 chars kept) |
| attempts | today | Runs started on this task |
| deliverable | today | File path or action the task produces; checked at finish |
| acceptance | today | 3 to 5 checkable criteria |
| routine | today | Preset routine key when the scheduler created it |
| created_at, updated_at | today | |
| notes | Phase 0 | Working notes: dated entries from `note_progress`, plus the automatic entry `_finalize` writes when a run ends without a clean finish (attempt number, how it ended, files written, last 3 tool calls, last error). Newest 4,000 chars kept |
| reassigned | Phase 0 | Times `update_task(assign_to=...)` moved it; 3 or more trips the `task_bouncing` watchdog pattern |
| goal_id | Phase 2 | The goal served |
| expected | Phase 2 | Expected effect |
| inputs | Phase 2 | What to read first |
| depends_on | Phase 2 | JSON list of task ids |
| due_at | Phase 2 | UTC due time from `due_days` |
| budget_steps, budget_tokens | Phase 2 | Per-task budgets |
| checks | Phase 2 | Tool checks for the Phase 3 gate |

### Scheduler rule for depends_on

Today the scheduler hands out every `todo` task in priority order, across companies, up to
`max_concurrent_runs`. In Phase 2 the dispatch query skips a task while any id in its `depends_on` has a status
other than `done` or `cancelled`. A cancelled dependency counts as satisfied so one dropped task cannot strand a
chain; the dependent task's prompt says which dependency was cancelled. The Tasks tab shows such tasks as
"waiting on #12". The CEO preset already advises chains of at most two steps; the scheduler does not enforce a
depth limit, and a cycle in `depends_on` is refused at `create_task`.

### File leases

Two agents can run at once (`max_concurrent_runs`), and today nothing stops both from writing
`site/index.html`. Phase 2 adds leases in `workspace.py`:

| Column | Meaning |
|---|---|
| company_id, path | The file, as a company-relative path |
| task_id, agent_id, run_id | Who holds it |
| expires_at | 30 minutes after the last write, as a safety net for runs that die |

`write_file` and `delete_file` take the lease on their path for the current run before writing, renew
`expires_at` on every write, and `agents._finalize` releases every lease of the run. A write to a path leased by
another live run returns:

    ERROR: site/index.html is being edited by Forge (task #12) until 14:35 UTC; work on something else or wait

An expired lease is taken over silently. Leases are per file, not per folder, and reads are never blocked.

## Rhythms

The table from the plan, then the mechanism behind each row. Routine tasks are created by the scheduler from
preset routines, as today (`scheduler._routines`: one task per routine when the last one is older than
`every_hours`, never two open at once, and not before the company is `first_after_hours` old).

| Cadence | Who | Output |
|---|---|---|
| Every run | Each agent | Reads the brain digest; ends with the structured handoff that exists today |
| Daily, morning | CEO | Plan: top 3 priorities per goal, tasks queued, blockers raised |
| Daily, evening | Analyst | Metrics pulled from connectors, anomalies flagged, one-page brief for the owner |
| Weekly | CEO + Analyst | Review: goal progress, experiment verdicts, decisions due for review, tasks cancelled; a board memo for the owner |
| Monthly | CEO | Strategy check against the numbers: keep, change or kill the company, sent to the owner as a decision |

### Today

| Rhythm | Mechanism today |
|---|---|
| Every run | `system_prompt` carries charter, memory and metrics; `finish(summary, status, deliverable, evidence, open_issues, next_step)` writes the handoff into `tasks.result` |
| CEO planning | `scheduler._maybe_cycle`: every `cycle_minutes` (default 60), 10 minutes after the team runs out of work, or 3 minutes after an owner decision (`cycle_requested`). `cycle_prompt` shows open tasks, finished work with deliverable checks, recent decisions, recent chat and files, and allows at most min(5, 12 - open) new tasks |
| Morning report | CEO routine, every 24 h (first after 20 h): `report_to_owner` writes under 200 words into the Chat tab |
| Weekly review | CEO routine, every 168 h (first after 160 h): appends to `docs/weekly-review.md`, using `ops/kpi-report.md` if it exists |
| Weekly KPI report | Analyst routine (Tally), every 168 h: `ops/kpi-report.md`, numbers typed in with `record_metric` |
| Daily QA review | QA routine (Sentry), every 24 h (first after 12 h): checks finished tasks against acceptance with tools, appends to `docs/reviews.md` |
| Monthly close | Finance routine (Ledger), every 720 h: `ops/close-<YYYY-MM>.md` |

Everything here is written by the model from what it can see, and it reaches the owner only in the dashboard.

### Planned (Phase 2): rhythms.py

For each rhythm, code computes the inputs, the model writes the prose, and the output is stored where the owner
will find it.

| Rhythm | Code computes (`rhythms.py`) | Model writes | Stored and sent |
|---|---|---|---|
| Every run | `brain.digest()`; working notes; "Connectors that are down right now" (Phase 1) | The handoff via `finish` | `tasks.result`, `runs.summary` |
| Daily morning plan (CEO routine, 24 h) | Active goals with current vs target; open tasks per goal; blocked tasks and pending escalations; pending brain proposals; experiments past their stop date | Top 3 priorities per goal; tasks created with `create_task(goal_id=...)`; blockers raised with `ask_decision` or `ask_owner`; proposals reviewed | Tasks and decisions; a short `report_to_owner` message only when something changed |
| Daily evening brief (analyst routine, or the CEO when the team has no analyst, 24 h) | For every metric with a connector source: latest value, delta vs yesterday and vs 7 days ago; anomalies (more than 50% change on a metric with at least 7 days of baseline); what shipped (tasks done in 24 h, with deliverables); what is stuck (blocked tasks, open escalations); decisions waiting, each with its recommendation | The one-page brief, from that data only. A number that is not in the data may not appear | `briefs` row written with `write_brief(kind=daily, body)` (`data` JSON = the computed inputs; `body` = the prose); `notify.send(kind=brief)` to Telegram; pinned on the Portfolio page; shown on the company Overview |
| Weekly review (CEO, with the analyst's data, 168 h) | Goal progress from metrics rows with a connector source (never from handoffs); experiments with verdicts due and their metric values; decisions whose `review_at` has passed; open tasks with no goal; the week's shipped and stuck lists; budget use (GPU hours, emails, posts) | Verdicts (`conclude_experiment`); outcomes on reviewed decisions; goals attached to tasks, or tasks left for cancellation; the board memo | `briefs` row (kind weekly, via `write_brief`); Telegram; Portfolio; the cancellation rule runs when the run ends |
| Monthly strategy check (CEO, 720 h) | Goal progress over the month; revenue and traction metrics; GPU hours and money spent; days since the last real-world result; the lessons list | A keep, change or kill recommendation with reasons | `briefs` row (kind monthly, via `write_brief`) and an `ask_decision` with options Keep, Change and Kill (pause the company), filed with `reversible=false` so it never resolves on its own |

The `briefs` table: `id, company_id (NULL for a portfolio-wide brief), kind (daily, weekly, monthly; Phase 4
adds portfolio), body, data JSON, created_at`. A portfolio-wide daily brief is assembled by code from the day's
per-company briefs and sent as one Telegram message; owner-experience.md describes what the owner sees.

## Verification

### Today

`finish(status=done)` on a task runs `_deliverable_ok`: the deliverable path must exist, must have been written
while working on this task (this session's `ctx.written`, or an earlier attempt's `file` events carrying the task
id), and must be at least 40 bytes. The first failure returns "Not finished yet: ..." and the agent may fix it;
the second forces status `partial` and appends the problem to open issues. The CEO's cycle prompt shows
`deliverable_status` for recently finished tasks (exists, size, age, or MISSING). The static checks
`audit_page`, `check_contrast` and `review_text` exist as tools, but nothing forces an agent to run them, and
the QA agent's daily review is a model reading files.

### Planned: the gate

| Kind of work | Gate | Built in | Needs |
|---|---|---|---|
| Web pages (deliverable under `site/`, or `checks` mentions `browser_check`) | Rendered in a headless browser: no console errors, every form has a target (`action` or `data-netlify`), same-site links resolve, mobile screenshot taken. A static fallback counts only when Playwright is not set up, and its result is labelled "Static check only" | Phase 3 (`verify.py`) | `browser_check` and the Playwright connector (Phase 1) |
| Code (deliverable under `code/`, or a test file exists) | Its tests run in the sandbox and pass | Phase 3 | `sandbox.py` (Phase 3) |
| Payment links (deliverable or handoff contains a Stripe payment link) | Retrieved from the Stripe API and active | Phase 3 | `stripe__get_payment_link` (Phase 1) |
| Outreach (`send_email` queued in this run) | Compliance: sender identity, opt-out line, daily cap, suppression list, consent flag for EU contacts, `review_text(kind=cold_email)`. The same checks run in `send_email` before the request is queued (Phase 3), so a trust-level-2 send cannot skip them; the gate re-checks every email queued in the run at finish | Phase 3 | `contacts` table (Phase 2) |
| Research (deliverable under `research/`) | Every http(s) URL cited in the file was fetched in this task (`tool` events "read <url>"), and at least one source is cited | Phase 3 | none |

How `finish()` applies it, in order:

1. `status` is normalised; anything other than done, partial, blocked or waiting becomes done.
2. If status is done and there is a task, the deliverable check (today's rule) runs first.
3. Then `verify.gate(ctx, task)` picks every kind that matches (a landing-page task that also queued an email
   runs two gates), runs each check, and runs every line in `task.checks`.
4. A failed gate returns "Not finished yet: <what failed>" once (`ctx.finish_warned`). On the second failure the
   status is forced to `partial` and the gate output is appended to open issues, exactly like the deliverable
   check today.
5. The gate output is stored with the handoff in `tasks.result`, so the CEO's review prompt and the QA agent
   see it.

The second-agent rule: a QA or CEO review covers only what tools cannot check (is the copy honest, does the page
say what the offer is, is this the right lead list). The review prompt carries the gate output and the tool
results, not just the draft, because a model re-reading a draft mostly agrees with it.

## Budgets and watchdog

### Budgets

| Budget | Setting | Default | Enforced where | Since |
|---|---|---|---|---|
| Steps per run | `max_steps_per_run` (global) | 14 (founding at least 26, cycle at least 18, chat at most 8) | The `agents.run` loop; at step `max_steps - 2` the model is told to save and finish | today |
| Attempts per task | `max_task_attempts` (global) | 2 | `agents._finalize`: after that many failed attempts the task is blocked and escalated (`task_failed_twice`) instead of retried. Today the limit is 3 and nothing escalates | Phase 0 |
| Emails per day | `daily_email_limit` (company; the plan's "emails per day") | 20 | `send_email`: emails sent today plus pending `send_email` approvals must stay under the limit, else "Daily email limit (N) reached" | today |
| Steps per task | `steps_per_task` (company) | = `max_steps_per_run` | A task run gets min(`steps_per_task`, `task.budget_steps`) | Phase 2 |
| GPU hours per day | `gpu_hours_per_day` (company) | 6 | `scheduler.tick`: a company whose runs today (sum of `runs` durations) exceed the budget gets no task or cycle dispatch until the next UTC day; its routine tasks still queue | Phase 2 |
| Posts per week | `posts_per_week` (company) | 10 | `social_post`: posts executed or pending this week must stay under the limit | Phase 2 |
| Money | `money_usd` (company) | 0, display only | `spend_money` is a locked policy (always ask). Every approved purchase is the owner buying it themselves; the Portfolio sums approved purchases | today (lock), Phase 2 (display) |
| Tokens per task | `task.budget_tokens` | none | Advisory: stored and shown | Phase 2 |

### Watchdog

`watchdog.tick()` runs on every scheduler tick (every 8 seconds) with cheap SQL and no model calls. Each pattern
has a dedupe key and carries a recommended answer, so the owner reads one question and taps one button. The
answers reuse five actions: Skip this task (cancelled), Retry once more with my note (todo, attempts reset),
Pause the company (paused), Retry founding (`agents.found`), Continue anyway (acknowledges; the company was
activated when founding ended and keeps working). "Keep going" and "Keep going without it" are recorded no-ops.

| Pattern | Trigger | Options (recommended marked *) |
|---|---|---|
| task_failed_twice | A task's last 2 attempts ended partial, error or out_of_steps. Raised directly from `agents._finalize` via `watchdog.task_failed(task, reason, run_status)`; the task is set to blocked | Skip this task* (recommended unless priority 1, then Retry is recommended), Retry once more with my note, Pause the company |
| repeated_error | 3 or more `error` events with the same normalised message within 1 h for one company | Pause the company*, Keep going |
| task_bouncing | A task reassigned 3 or more times (`tasks.reassigned`) | Skip this task, Retry once more with my note* (the note says who should own it), Pause the company |
| connector_down | A connector's Doctor row is bad on 2 consecutive checks (once per connector per day) | Keep going without it* (plans already avoid it: Phase 1 lists it as down in every prompt), Pause the company |
| stalled | Company active, autopilot on, open tasks exist, no run started in 2 h, and the model server is unreachable or every run in the last 2 h errored. Question: "Agents are stuck: <reason>", with the Doctor page as the fix | Pause the company*, Keep going |
| founding_partial | A founding run ends partial or out_of_steps; raised from `agents._finalize` via `watchdog.founding_ended(company, run_status, summary)`. The company is active while the question is pending | Continue anyway* (recommended when a charter exists, else Retry founding), Retry founding, Pause the company |

Dedupe: a pending question with the same pattern key (and task id where it applies) is not created again, and
a resolved one is not recreated within 6 hours; the watchdog remembers resolved keys in memory and finds them
again in the approvals history after a restart.

### What an escalation is

An escalation is an ordinary `approvals` row, so it uses the inbox, the Telegram buttons and `actions.decide`
that exist today:

| Column | Value |
|---|---|
| action | `question` |
| agent_id | The company's CEO |
| task_id | The task concerned, when there is one |
| summary | The question, first 140 chars |
| payload | `{question, options: [str], recommended: str, context: str, pattern: key, task_id?, company_id}` |
| status | pending, then answered |

Rows are created by `watchdog.escalate(company, pattern, question, options, recommended, context, task_id)`,
which applies the dedupe rule and returns the approval id, or None when an equivalent question is already
pending or was resolved less than 6 hours ago.

Answering keeps today's behaviour (a memory note of the answer; a waiting or blocked task reopened with the
answer appended to its description; a CEO cycle requested) and additionally applies the pattern action through
`watchdog.resolve(row, answer)`. The Inbox renders `options` as buttons plus the free-text answer; Telegram
renders one inline button per option. `ask_owner` uses the same row today with a question and no options;
Phase 2 decisions use the same table with their own action.

## Decisions vs approvals

Approvals are "may I send this?". Decisions are "which of these should we do?". They are different rows in the
same `approvals` table so that one inbox, one Telegram poller and one `actions.decide` handle both.

### Today: approvals and questions

| Kind | `action` values | Payload | Owner's options |
|---|---|---|---|
| Approval | send_email, social_post, deploy_site, http_post, run_code, spend_money | The action's arguments (`to`, `subject`, `body`; `platform`, `content`, `link`; `folder`; `method`, `url`, `body`, `headers`; `language`, `code`, `timeout`; `item`, `amount_usd`, `vendor`, `url`, `reason`) | Approve, after editing `to`, `subject`, `body`, `content` or `code`; or Reject with an optional note. A rejection with a note creates a priority-1 "Revise: ..." task for the agent |
| Question | question | `{question}` | A free-text answer |

`actions.gate` routes each action by policy (`approve`, `auto` or `off`, per action, globally or per company;
`spend_money` is locked to `approve`). Identical pending payloads are deduplicated. Every owner decision writes
a memory note, reopens a waiting or blocked task, and requests a CEO cycle within 3 minutes.

### Planned (Phase 2): decisions

A decision is an `approvals` row with `action = 'decision'` and this payload:

| Field | Meaning |
|---|---|
| question | What must be decided, in one sentence |
| options | List of short option strings |
| recommended | One of `options` |
| deadline_at | UTC time after which a reversible decision resolves itself |
| reversible | true when the recommended option can be undone later |
| context | Why now, with the numbers that matter |

It is filed by the CEO with `ask_decision(question, options, recommended, deadline_hours=48, reversible=true)`;
by `propose_brain_change` when a proposal touches price, offer, positioning or ICP; by `set_goal` when the goal
set changes; by the monthly strategy check; by the Phase 3 trust ladder when it suggests a promotion; and by
the Phase 4 portfolio review. `ask_owner` keeps working for simple questions.

Deadlines and auto-resolution: on every tick the scheduler looks for pending decisions whose `deadline_at` has
passed. A reversible one is resolved with its `recommended` option: the approvals row becomes `answered` with
that option as the result, a `decisions` row is written with `decided_by = 'deadline'`, the agent receives the
answer exactly as it would an owner's answer, and the owner is notified ("Decided by deadline: ..."). An
irreversible decision waits for the owner for as long as it takes, and the daily brief lists it every day as
overdue. An owner's answer writes the same `decisions` row with `decided_by = 'owner'`.

The Inbox shows Decisions in their own section above Approvals, each with one button per option, the
recommended one highlighted, and the deadline and reversibility shown. Telegram sends the same buttons
(`q:<approval_id>:<option index>`). Both answer through `POST /api/approvals/<id>` with `decision = 'answer'`
and the chosen option as `answer`, so a click in the dashboard and a tap on the phone take the same path.

What never becomes a decision: anything the trust ladder keeps at "always ask" (spending money, irreversible
actions, legal commitments, the first message to a new contact) stays an approval, with the real payload the
owner can read and edit. See safety.md.
