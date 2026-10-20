# Debugging a company

How to find out why a company is stuck or misbehaving. Read this before changing code: most "bugs" are Holdco
waiting on purpose (a budget, the owner, a dependency, the model server) and say so somewhere.

## 1. Is it waiting on purpose? Check in this order

| Check | Where | What it means |
|---|---|---|
| Kill switch | sidebar, `GET /api/state` `kill_switch` | Every agent stopped; outbound actions frozen until Resume |
| Autopilot | sidebar switch, setting `scheduler_enabled` | No dispatch or cycles; chat still works |
| Company paused | company header, `companies.status` | `paused` gets no work; Holdco pauses companies itself only for a watchdog or review answer ("Pause the company", "Shut down") |
| Model server down | Doctor row `model_server` | The scheduler holds all dispatch and rechecks every few minutes; the feed says "waiting for the model server" |
| A Bench is running | Settings "Test this model", `GET /api/bench/status` | Dispatch and cycles wait while `bench.RUNNING` |
| Over budget | Doctor row `token_budgets`; Overview "GPU budget used" | The daily token budget (counted from local midnight, owner chat excluded) or `gpu_hours_per_day` (counted from UTC midnight) is used up: no agent tasks or cycles until the day turns; owner tasks and owner-requested cycles still run |
| Waiting on the owner | Inbox; task status `waiting`; handoff line "Waiting on the owner: #N" | The task reopens when #N is answered; the agent cannot continue without it |
| Waiting on a task | Tasks tab "waiting on #12" (`blocked_by`) | Dispatched only when every dependency is done or cancelled |
| Research in quiet hours | company setting `quiet_hours` | Low-value research (role research or discovery, priority 3, no due date) runs only in the quiet window unless nothing else can start |
| No planning cycle | feed, `companies.cycle_backoff` | Change-driven planning: with nothing new, cycles space out (1, 2, 4, 8 hours). An owner message, answer or setting change triggers one; "Plan now" forces one |
| Founding stage | company status `founding`, Overview | Staged founding (research, charter, critic, plan); a stage that ended badly raises `founding_partial` |

## 2. The watchdog's questions (Inbox and phone)

Each stuck pattern becomes one question with options and a recommendation (`holdco/watchdog.py`). Answering applies
it at once (`watchdog.resolve`).

| Pattern | Raised when | Usual cause to look for |
|---|---|---|
| `task_failed_twice` | a task's failed attempts reach `max_task_attempts` (2); the task is blocked | the last two run transcripts: out of steps (task too big, wrong tools on the belt), the same refusal twice, a missing input |
| `founding_partial` | a founding stage ended partial or out of steps | search down during research, a charter that failed validation, a plan stage that queued no tasks |
| `repeated_error` | 3 identical error events within an hour | the error text in the feed; often a connector or a bad setting |
| `task_bouncing` | a task reassigned between roles 3 times | unclear task (no deliverable or acceptance), or no role has the tools it needs |
| `connector_down` | a connector failed two Doctor checks in a row (web search counts) | the Doctor row's detail: an expired key, a changed vendor API, the network |
| `stalled` | open tasks, autopilot on, no run in 2 hours, and the model server is bad or the last 3 runs errored | the Doctor's model rows; the last runs' `summary` |

## 3. The Doctor (`GET /api/doctor`, Doctor page)

Rows: `model_server`, `model` (the default model is installed), `gpu_fit` (the model fits in GPU memory; "spilling
into RAM" makes everything slow), `search` (which provider works), `connector:<id>` (each connected service),
`sandbox`, `disk`, `backup`, `errors_24h` (the most common errors), `search_index`, `token_budgets`. Each row has a
fix. "Run checks" refreshes them.

## 4. What did the agent actually do?

| Question | Look at |
|---|---|
| Its reasoning and every tool call, live | the company's Thinking tab |
| A finished session in full | Team tab sessions, or `GET /api/runs/<id>` (`transcript`: messages, tool calls, results; `summary`; `status`; `max_prompt_tokens`) |
| What the task's attempts tried and what is left | `tasks.notes` (working notes) and `tasks.result` (handoffs, Holdco's notes, "[Update]" lines from the owner's answers) |
| What the agent was told about the world | the facts block: `records/STATUS.md` in the company folder (also `emails.md`, `outbound.md`, `owner.md`) |
| What it may not do and why it was refused | results starting with `ERROR:` or "Not finished yet:" in the transcript; refusals by the claims gate also appear in the feed as `claims` events |
| What ran outside | the audit log (Inbox page): action, who proposed, who approved (`owner`, `policy:auto`, `trust:<level>`, `mcp:<client>`), payload, result |
| Why an action asked instead of running | the request card's "why you are asked" line (`_why_asked`, `_taint` in the payload) |
| What the verification gate found | the task modal's gate results (`tasks.verification`) and the `[gate: ...]` part of the CEO's planning prompt |
| How full the context window got | Settings "Context use" (p50/p95), `runs.max_prompt_tokens` |

## 5. Questions answered with SQL

Open `data/holdco.db` read-only (`sqlite3 -readonly data/holdco.db`) or a copy; never write to a running instance's
database by hand.

```sql
-- the company's open work and why it may be held
SELECT id, title, role, status, priority, goal_id, depends_on, due_at, attempts FROM tasks
 WHERE company_id=? AND status IN ('todo','in_progress','blocked','waiting') ORDER BY priority, id;
-- the last sessions and how they ended
SELECT id, kind, task_id, status, steps, tokens, started_at, finished_at, substr(summary,1,200) FROM runs
 WHERE company_id=? ORDER BY id DESC LIMIT 20;
-- what waits on the owner
SELECT id, action, status, summary, created_at FROM approvals WHERE company_id=? AND status='pending';
-- errors in the last day
SELECT created_at, message FROM events WHERE company_id=? AND kind='error' ORDER BY id DESC LIMIT 30;
-- numbers and where they came from (manual = typed by an agent; it never scores)
SELECT name, value, source, created_at FROM metrics WHERE company_id=? ORDER BY id DESC LIMIT 30;
-- tokens toward today's budget: runs other than owner chat since local midnight (planning.tokens_today)
SELECT SUM(tokens) FROM runs WHERE company_id=? AND kind != 'chat' AND started_at >= ?;  -- ? = local midnight in UTC
```

## 6. Reproduce and fix

- **Code bug.** Write a failing test first, with the scripted model (`tests/fake_ollama.py`) and the fakes; see
  [development.md](development.md). Name review or seam fixes after the defect.
- **Model behaviour.** Check whether a Bench scenario covers it: `python -m holdco.bench --model <model> --only
  <scenario>`; each result keeps a trace of what the model did. Otherwise reproduce in a sandbox Holdco (its own
  `HOLDCO_DATA` and port, never the owner's) and drive it through the API. Fix it in general, not for one case: a
  clearer tool description, a refusal that says what to do, a tool on the belt, a guard that stops blocking
  legitimate work. Then add a replay test (`tests/test_phase*_realmodel.py`). One run of a small model is a sample,
  not a verdict: rerun before concluding.
- **Windows-only CI failure.** Usually timing: see the Windows rules in [development.md](development.md).
