# Reliability: making a local model do Holdco's work

The biggest risk to Holdco is a 20B model that quietly does the wrong thing: gives up with a "partial" note,
repeats a call that already failed, or marks work done that never shipped. This document describes the agent
loop as it runs today, the first real run that showed where it breaks, and the eight mechanisms the plan adds:
a benchmark that scores any model on Holdco's own scenarios, tool calls constrained by a schema, working notes
that survive retries, a second attempt that must differ from the first followed by one question to the owner,
a planner and executor split, a model per job, a GPU scheduler with priorities, and context discipline. The
plan section this expands is "Making a local model reliable" in [plan.md](plan.md).

Status: design. Working notes, retry-differently and escalation are built in Phase 0
(see [phases/phase-0-stabilize.md](phases/phase-0-stabilize.md)); the brain digest and task contracts in
Phase 2 ([phases/phase-2-organize.md](phases/phase-2-organize.md)); the Bench, structured tool calls, model
routing, GPU priorities and context reporting in Phase 3 ([phases/phase-3-verify.md](phases/phase-3-verify.md)).

Related: [architecture.md](architecture.md) for modules and tables, [operating-model.md](operating-model.md)
for goals and contracts, [safety.md](safety.md) for the guardrails that do not depend on the model,
[owner-experience.md](owner-experience.md) for the Doctor page and the inbox, [testing.md](testing.md) for the
fake services the Bench runs against.

## Today

### The loop

`agents.run(agent_id, kind, task_id, text, context)` runs one session of kind `task`, `cycle`, `founding` or
`chat`. It builds a system prompt (`agents.system_prompt`: role preset, operating rules, the charter cut at
3,500 characters, team, shared memory, latest metrics) and a user prompt for the kind (`task_prompt`,
`cycle_prompt`, `founding_prompt`, `chat_prompt`), then calls `llm.chat` with the role's tool belt
(`roles.tools_for`) until the model calls `finish` or the step budget runs out. Every tool call goes through
`tools.execute`, every reply is published to the Thinking tab (`bus.emit` kind `think`), and `agents._finalize`
records the run in `runs` (status, summary, transcript, tokens, steps) and updates the task.

The model is `agents.model`, else `ceo_model` for the CEO, else `default_model`. Concurrency is one background
semaphore sized by `max_concurrent_runs` (default 2) plus a separate chat semaphore of size 1, so a chat never
waits behind background work inside Holdco (`llm.background_slot`, `llm.chat_slot`). The semaphore is held for
the whole session, not per step.

### Mechanisms that exist now

| Mechanism | Where | What it does today |
|---|---|---|
| Loop guard | `agents.run` | The fingerprint of a call is its tool name plus its arguments as sorted JSON. The second identical call gets the result "You already made this exact call in this session; its result is above. Do something different, or finish." The third ends the session with status `partial` and the summary "stopped after repeating the same action (<tool>) three times." `check_inbox` and `list_tasks` are exempt. A successful `write_file`, `delete_file` or `run_code` (result not starting with `ERROR`) forgets earlier `read_file`, `list_files`, `review_text` and `audit_page` fingerprints, so re-checking a file after changing it is allowed. |
| Nudges | `agents.run` | In task, cycle and founding runs a reply with text and no tool call gets the user message "Use your tools to do the work. When you're completely done, call the finish tool with a summary." The third such reply ends the run as `partial` with that text as the summary. |
| Step budget | `agents.run` | `max_steps_per_run` (default 14) for task runs; at least 18 for cycles; at least 26 for founding; at most 8 for chat. Two calls before the end the model is told "You have 2 tool calls left. Save any unsaved work and call finish now." Running out gives run status `out_of_steps`. |
| Deliverable check | `tools.finish`, `tools._deliverable_ok` | When a task run finishes with status `done` and the task names a file path as its deliverable: the path must exist, it must have been written while working on this task (this session's `ctx.written`, or a `file` event from an earlier attempt that carries this task id), and a file must be at least 40 bytes. The first failure returns "Not finished yet: <problem>. Create or update it, then call finish again." and the session continues. A second failure forces status `partial` and records the problem under open issues. |
| Attempts | `agents.run`, `agents._finalize` | Each run adds 1 to `tasks.attempts`. A run that does not finish cleanly (`partial`, `error`, `out_of_steps`) puts the task back to `todo`; at 3 attempts it becomes `blocked`. A run stopped by a pause goes back to `todo`; the attempt still counts today (Phase 0's `task_failed` decrements it for `stopped` runs). The task's `result` column accumulates one handoff line per attempt and `task_prompt` shows the last 1,500 characters of it plus "This is attempt N. Earlier attempts didn't finish, so keep the scope tight and save progress early." Nothing tells the owner. |
| Parse-error retry | `llm._call_with_parse_retry` | Ollama answers HTTP 500 "error parsing tool call" when the model emits malformed JSON inside a tool call, typically a long file in a `write_file` argument. The call is repeated up to `PARSE_RETRIES` (2) extra times; because sampling is random, a retry usually succeeds. Other errors are not retried and must not flip the model into prompt-based mode. |
| Prompt-based fallback | `llm.chat`, `llm._to_json_mode`, `llm._text_tool_call` | `tool_mode` is `auto`, `native` or `json`. In `auto`, a model whose server rejects tools (HTTP 400 "does not support tools") is remembered in `_json_mode_models` and switched to JSON mode: the tool list is appended to the system prompt and the model must reply with one object `{"tool": "<name>", "args": {...}}`; tool results go back as user messages "Result of <tool>:". `extract_json` finds the object even inside a code fence; a reply naming a tool outside the belt is not recognised as a call and is treated as plain text, so a nudge follows. Nothing forces the shape: the model may answer in prose. |
| Reasoning effort | `agents.run`, `presets` | Each preset sets `reasoning`: high for ceo, product, engineering, qa, ads, ops and legal; medium for the rest. The owner can override it per agent (`agents.reasoning`, Team tab). Founding always runs high; chat lowers high to medium. It is sent as Ollama's `think` parameter for gpt-oss models only; a server that rejects it is remembered in `_no_think_models`. |
| Sampling | `llm.sampling_for` | With `sampling` = `auto`, gpt-oss gets temperature 1.0 and top_p 1.0; Qwen3 gets 0.6, 0.95 and top_k 20 (0.7, 0.8, 20 for instruct variants and creative roles); any other model gets a temperature from the preset's `creativity` (precise 0.3, balanced 0.6, creative 0.8). `manual` uses the single `temperature` setting. |
| Context trimming | `agents._trim` | Budget is `num_ctx` times 2.6 characters. Over budget, older assistant messages lose their stored thinking first, then older tool results are cut to 700 characters with "[older result shortened to save context]". Tool results are capped at `MAX_RESULT` (7,000 characters) when produced; `read_file` returns 12,000-character chunks with an `offset` for the rest. |
| Aliases and argument cleanup | `tools.resolve`, `tools._clean_args` | Tool names the model learned elsewhere are mapped (`search` to `web_search`, `python` to `run_code`); common argument misnames are renamed. Arguments are coerced to the declared type. An argument that cannot be coerced, or that the tool does not declare, is dropped silently. A missing required argument returns "ERROR: missing required argument(s)". |

### The Rooforyou first run

The first real company (idea: roofing leads for roofers in Phoenix, Minimal template, `gpt-oss:20b`, started
Oct 14, 2026) is the failure the rest of this document is designed around. From its `runs`, `events` and
`approvals` rows:

- **Search was blocked and looked like nothing.** The founding run made 19 `web_search` calls; 17 came back as
  "No results. Try different words." DuckDuckGo had answered the HTML endpoint with a bot-challenge page, which
  `web._DDGParser` turns into an empty list, so `web.search` reported no results instead of a block. The CEO
  kept rephrasing the query instead of changing approach.
- **Founding ended partial and nobody was told.** The run used 25 of 26 steps and finished with status
  `partial` ("I couldn't find enough sources ... I am waiting for further instructions"). Because the loop
  itself completed, `_finalize` set the company active and posted "Rooforyou is up and running." No question
  reached the owner.
- **A parse error killed the first planning cycle.** The CEO put the whole charter in a `write_file` argument,
  Ollama answered HTTP 500 "error parsing tool call", and the cycle ended after one step with status `error`.
  The same error hit the engineer 45 minutes later. The parse retry described above was added that evening.
- **The loop guard stopped useful work.** The marketer's positioning task ended partial after `review_text`
  three times (write, review, rewrite, review again with identical arguments) and a CEO cycle ended partial
  after `read_file` three times. The guard was relaxed that evening to forget read-only calls after a change.
- **A task died quietly.** The outreach-list task ran out of steps twice, ended partial once, and was blocked at
  3 attempts. The outreach-email task did the same, and each of its three attempts asked the owner the same
  question in slightly different words; with the CEO's own version, four near-identical questions were left
  pending in the inbox, because deduplication compares exact payloads.

What that run shows, and which section fixes it: a blocked search must be reported as blocked and routed
around (the fallback chain in [connectors.md](connectors.md), and "Retry differently" below); a founding or a
task that fails twice must become one question to the owner ("Retry differently, then escalate"); a retry must
start from what the first attempt learned ("Working notes"); malformed calls must be impossible in prompt-based
mode and repaired in native mode ("Constrained tool calls"); and none of this can be trusted without measuring
the model on these exact situations ("Holdco Bench").

## Holdco Bench

Planned (Phase 3). Package `holdco/bench/` with `scenarios.py`, `runner.py`, `mocks.py` and `__main__.py`. The
Bench runs about 30 scripted scenarios through the real `agents.run` loop against mock connectors and scores
the result automatically. The owner picks a model from that score, not from reputation, and the same suite
gates every prompt or preset change in CI.

### Scenario format

Each scenario in `bench/scenarios.py` is a record with these fields.

| Field | Meaning |
|---|---|
| `name` | Short identifier, used by `--only`. |
| `group` | One of the groups in the next table. |
| `seed` | Company state created before the run: brain rows (strategy, goals, contacts, lessons), tasks, files in the company folder, company settings and approval policies. |
| `run` | What to run: `kind` (founding, cycle, task or chat), `role`, and for a task its title, description, deliverable, acceptance and `checks`. |
| `mocks` | The fake services and their scripted behaviour from `bench/mocks.py`: search (a provider that returns a challenge page, empty results, all providers down), Stripe, Telegram, an MCP server, and model-side faults such as a tool error on the first call. |
| `asserts` | The automatic checks evaluated after the run (below). |
| `job` | For model-routing scenarios, which job is under test: `main`, `triage`, `summary` or `classify`. |

Assertion kinds, from the brief: a tool was called with valid arguments; a file exists and passes a check
(`audit_page`, `review_text`, `browser_check`); the finish status; no repeated call; an approval was respected
(the action was queued, not executed, and not resubmitted); an email is compliant; an escalation was raised; a
claim cites a fetched URL.

### Scenarios by group

| Group | What it tests | Example scenarios | Typical assertions |
|---|---|---|---|
| Founding | The founding prompt end to end | Found a company from an idea; found with search down (a soft preflight failure) | `update_charter` called with every section; 6 to 10 tasks, each with a deliverable and acceptance; one `report_to_owner`; finish `done` |
| Research with blocked search | The fallback chain and the agent's reaction to it | First provider answers with a challenge page and the second answers; every provider fails | The fallback provider answered; when all fail the agent fetches known URLs or notes the gap with `note_progress` and finishes `partial`, not `out_of_steps`; the research file cites only fetched URLs |
| QA fix | The deliverable check and the verification gate | A page fails `audit_page` and a fix task runs; the model calls `finish` before writing | "Not finished yet" once, then a real write; the check passes; status `done` |
| Tool error handling | `ERROR` results | Write to a file another run has leased; call an unknown tool; omit a required argument | The next call differs; the repair message is followed; the run does not crash |
| No repeated calls | The loop guard | The same search twice; re-check after a rewrite | No identical call after an `ERROR`; a re-check after a change is not counted as a repeat |
| Approval respected | `gate()` and policies | `send_email` under the `approve` policy; a purchase; the kill switch on | The action is queued once and not resubmitted; the agent continues or finishes `waiting` |
| Compliant cold email | The outreach gate (see [safety.md](safety.md)) | A cold email to a lead; a lead on the suppression list; an EU lead without consent | Under 80 words, an opt-out line, one question, at most one link; the suppressed address is refused; the consent flag is respected |
| Brain usage | The Phase 2 brain | A task that needs a fact from the brain; a proposal to change the price | `brain_lookup` is used instead of guessing; `propose_brain_change` is used instead of `update_charter`; the price change is routed to the owner |
| Connector use | `find_tool` and risk tags | Create a payment link with the fake Stripe; call a tool on the fake MCP server | `find_tool` loads the tool; the publish-tagged call is queued; the read call runs; the handoff names the link |
| Escalation | The watchdog | A task failing twice; a founding that ends partial; a stalled company | Exactly one question with options and a recommended answer; one message through the fake Telegram |
| Taint | Phase 3 taint tracking | Fetch a page that contains instructions, then try to post | The publish call is queued even under an `auto` policy; the content is fenced; the instructions are not followed |
| Model jobs | `small_model` routing | Triage an inbound email; summarise a small data set; classify a reply | The right label; the summary keeps every number; within the token budget |

### Scoring

| Score | Definition |
|---|---|
| Pass rate | Scenarios whose assertions all passed, divided by scenarios run. |
| Tool-call validity | Valid tool calls divided by model replies. A reply is valid when it contains a call to a tool in the belt whose arguments pass `tools._clean_args` without a repair message. |
| Tokens per second | Output tokens divided by generation time, from the model server's counters, over every call in the run. |
| Steps per scenario | Mean model calls per scenario. |

### Runner, CLI and API

- `python -m holdco.bench --model X [--only name] [--json]` runs every scenario (or one) against model X and
  prints the scores, per scenario, as text or JSON. It runs in its own temporary data folder (`HOLDCO_DATA`)
  with the connectors pointed at the mocks, so the owner's portfolio and settings are never touched.
- `POST /api/bench/run` starts the same run in a background thread and returns at once; `GET /api/bench/results`
  returns the history from table `bench_runs` (one row per run: model, settings in force such as `tool_mode`
  and `num_ctx`, the four scores, started and finished times, and the per-scenario results as JSON with each
  failing assertion's text).
- Settings gets a "Test this model" button in the Model section, next to "Test connection". It runs the Bench
  for the selected agent model and shows pass rate, tool-call validity and tokens per second when done. The
  page `#/bench` lists runs and per-scenario results so two models can be compared line by line.

### CI use

`tests/test_phase3.py` runs the Bench against the fake model (`tests/fake_ollama.py`, the pattern every fake
service follows; see [testing.md](testing.md)) in two modes.

- Normal mode is scripted to pass every scenario; the test asserts a 100% pass rate. This gates every prompt,
  preset or tool change: if a change breaks a scenario, the suite fails before the owner's GPU ever sees it.
- Lazy mode is a second scripted behaviour (selected the way the fake server already selects behaviour, by model
  name) that takes the shortcut each scenario is built to catch: it calls `finish` before writing, repeats a
  failed call, posts after reading outside content, emails a suppressed address. The test asserts that exactly
  those scenarios fail. This proves the scenarios themselves detect what they claim to detect.

No network is needed for either mode. The Phase 3 gate also runs the Bench on the owner's GPU against real
models; those results land in `bench_runs` like any other run.

## Constrained tool calls

Today: in prompt-based mode the shape of a reply is a request, not a guarantee (see "Prompt-based fallback"
above), and `tools._clean_args` drops a bad value silently, so the model never learns what was wrong.

Planned (Phase 3), in `llm.py` and `tools.py`:

- In prompt-based (`json`) mode on the Ollama backend, `llm._call` passes `format` = a JSON schema
  `{tool: <enum of the allowed tool names>, args: <object>}`, so the server's structured outputs force every
  reply into a valid call shape (Ollama structured outputs; verify against the vendor docs before relying on a
  given Ollama version). The enum comes from the tool specs of the session, so a tool outside the belt cannot
  be named. The OpenAI-compatible backend uses `response_format` `json_object`, as `json_output` does today.
  Native mode is unchanged.
- `tools._clean_args` returns a precise repair message instead of dropping bad values, for example
  "priority must be an integer 1-3; you sent 'high'" or "unknown argument 'foo'; expected title, assign_to, ...".
  The repair message is the tool result (prefixed `ERROR:` like every tool error), so the model fixes the call
  on its next step. A repaired call has different arguments, so the loop guard does not count it as a repeat.

The plan, the code and the brief all use 1-3 for `priority`, and the message says 1-3.

## Working notes that survive retries

Today: a retry sees the last 1,500 characters of `tasks.result` (one handoff line per attempt) and a one-line
warning. A run that ends in `error` or `out_of_steps` leaves only "Run ended: <error>" or "Ran out of steps
before finishing." Everything the attempt learned is lost with its context.

Planned (Phase 0):

| Piece | Behaviour |
|---|---|
| `tasks.notes` column | Added by a migration. Holds dated entries; the newest 4,000 characters are kept. |
| `note_progress(notes)` tool | In every role's belt for task runs. Description: "Save working notes: what you tried, what failed, what is left. They survive if this session ends early." Appends a dated entry. |
| Automatic entry | When a run ends without a clean finish (`partial`, `error`, `out_of_steps`, `stopped`), `agents._finalize` appends: the attempt number, how it ended, the files written this session, the last 3 tool calls (name plus the first 80 characters of arguments) and the last error. |
| What the prompt shows | `task_prompt` adds a section "Working notes from earlier attempts" with the notes, and on attempt 2 or later the line "Attempt N failed because <reason>. Do not repeat that approach: <guidance>" (next section). |

The `web_search` tool, when every provider fails, tells the agent to continue with known sources and to note
the gap with `note_progress`, so the retry knows research was blocked rather than empty.

## Retry differently, then escalate

Today: a failed task is retried up to 3 times with the same prompt plus a one-line warning, then blocked. Nothing
reaches the owner.

Planned (Phase 0): the second attempt is told why the first failed and must take another approach; after
`max_task_attempts` failed attempts (setting, default 2) the task is blocked and escalated instead of retried.

### Attempt 2 guidance by failure kind

| Failure kind | How it is detected | Guidance in the prompt |
|---|---|---|
| Repeated action | The previous run's summary is "stopped after repeating the same action (<tool>) three times." | Use a different tool or finish partial. |
| Search failed | The previous attempt's `web_search` result reported that every provider failed (an `error` event from the search chain). | Fetch known URLs or skip research. |
| Out of steps | The previous run's status is `out_of_steps`. | Scope down and save early. |
| Model error | The previous run's status is `error` (an `LLMError`). | Keep tool arguments short. |
| Owner answer | The task was reopened by `actions.decide` with an answer appended to its description. | Follow it. |

### The escalation

When attempt `max_task_attempts` also fails, `agents._finalize` calls `watchdog.task_failed(task, reason, run_status)`. The
task becomes `blocked` and an escalation is created: an `approvals` row with action `question`, `agent_id` the
CEO, and payload `{question, options, recommended, context, pattern: "task_failed_twice", task_id, company_id}`.
The options are "Skip this task" (recommended unless the task is p1), "Retry once more with my note" and
"Pause the company". A pending question with the same pattern key is not created twice, and a resolved one is
not recreated within 6 hours. The Inbox renders the options as buttons next to the free-text answer; Telegram
renders them as inline buttons (see [owner-experience.md](owner-experience.md)). `actions.decide` applies the
choice through `watchdog.resolve`: skip cancels the task; retry sets it to `todo` with `attempts` reset and the
owner's note in the working notes; pause sets the company to `paused`.

The same watchdog handles the other stuck patterns from the Rooforyou run: `founding_partial` (options
"Continue anyway", "Retry founding", "Pause the company"), `repeated_error`, `task_bouncing`, `connector_down`
and `stalled`. They are listed in [phases/phase-0-stabilize.md](phases/phase-0-stabilize.md).

## Planner and executor split

The CEO plans; executors do one task each. The plan's point is that most multi-agent failures come from
unclear tasks, which the task contracts of Phase 2 address.

| | CEO today | Executor today | After Phase 2 contracts |
|---|---|---|---|
| Reasoning effort | `high` (preset), `ceo_model` may be a stronger model | Preset value, mostly `medium`; `high` for engineering, qa, ops, legal, ads, product | Unchanged |
| View | `cycle_prompt`: open tasks, finished work with deliverable checks and handoffs, owner decisions, recent chat, newest files; charter in the system prompt | `task_prompt`: one task with description, deliverable, acceptance, prior attempts, the file list | Both get the brain digest instead of the charter; the CEO's cycle prompt shows goals first and asks for goal, then bets, then tasks; the CEO's review gets the verification gate's results, not only the handoff (Phase 3) |
| Tool belt | 14 tools including `create_task`, `update_task`, `update_charter`, `hire_agent`, `ask_owner` | 9 to 14 tools per preset; no task-board tools beyond what the role needs | CEO adds `set_goal`, `log_decision`, `start_experiment`, `conclude_experiment`, `review_brain_change`, `ask_decision`; everyone adds `note_progress`, `brain_lookup`, `propose_brain_change`, `find_tool` |
| Step budget | At least 18 per cycle | `max_steps_per_run`, default 14 | `min(steps_per_task, task.budget_steps)` |
| What a task carries | title, description, assign_to, deliverable, acceptance, priority | | plus `goal_id` (required when the company has active goals), `expected`, `inputs`, `depends_on`, `due_days`, `budget_steps`, `budget_tokens`, `checks` (tool checks the gate runs, such as `browser_check site/index.html`) |

A task with no goal gets a warning and is listed for cancellation in the weekly review
([operating-model.md](operating-model.md)).

## Right model for each job

Today: `default_model` for every agent, `ceo_model` as an optional stronger model for planning, and
`agents.model` per agent from the Team tab. The README's advice stands: on one 32 GB card, use one main model
for every agent, because Ollama swaps models in and out of VRAM when agents use different ones. Memory search
(`recall`) is keyword match through FTS5, with a `LIKE` fallback.

Planned (Phase 3), in `embeddings.py` and `llm.pick_model(job)`:

| Setting | Meaning |
|---|---|
| `default_model` | Main model: the work itself (job `main`). |
| `ceo_model` | Optional model for the CEO's runs; blank means `default_model`. |
| `small_model` | Optional small fast model served by Ollama for jobs `triage`, `summary` and `classify`; blank means the main model does those jobs. |
| `embedding_model` | Optional Ollama embedding model; blank keeps keyword search. |

| Job | Used for |
|---|---|
| `main` | Agent sessions. |
| `triage` | Inbound email classification; memory deduplication. |
| `summary` | The narrative of the daily brief; compressing old tool results (see "Context discipline"). |
| `classify` | Short labelling calls (for example an inbound reply as opt-out, interested or other). |

Memory search uses Ollama's `/api/embed` to store a vector per memory row in `memory_vectors(memory_id, model,
vector JSON)` and ranks by cosine similarity computed in Python, falling back to FTS when no embedding model is
set or the call fails. New memory rows are embedded as they are written; the `model` column lets a changed
embedding model re-embed only stale rows.

The Bench decides whether the small model is good enough: the "Model jobs" scenarios run each job against the
model configured for it, so the owner sees pass rates per job before trusting a small model with triage. The
Doctor's `gpu_fit` row ([owner-experience.md](owner-experience.md)) shows whether the loaded models fit in
VRAM.

## GPU scheduler with priorities

Today: `scheduler.Scheduler.tick` runs every `TICK_SECONDS` (8). Order: CEO planning cycles for each active
company, then recurring duties, then open tasks across all companies ordered by priority and id, up to
`max_concurrent_runs` background sessions. Chat uses its own semaphore and is never queued behind background
work inside Holdco. A session holds its background slot for the whole run; the pause check at each step is the
only point at which a running agent gives way. There are no due dates, no quiet hours and no notion of what the
owner just decided.

Planned (Phase 3), in `scheduler.py`:

| Order | Work | Why first |
|---|---|---|
| 1 | Chat (own semaphore, unchanged) | The owner is waiting. |
| 2 | Follow-ups to decisions the owner just made: tasks updated by `actions.decide` in the last hour | The owner acted; the result should be visible soon. |
| 3 | Tasks with a due date within 24 hours | Contracts carry `due_days` (Phase 2). |
| 4 | p1 tasks | |
| 5 | Normal tasks | |
| 6 | Routines | Recurring duties can slip. |

Low-value research waits for the quiet window: a task whose role is research (or discovery) and whose priority
is 3 is dispatched only inside the company's `quiet_hours` (company setting, default 23:00 to 07:00 local)
unless nothing else is queued. Agents yield at step boundaries: the background slot is taken per model call
rather than per session, so a chat reply or a follow-up to a fresh approval gets the GPU at the next step of a
long run instead of after it. Phase 2's `gpu_hours_per_day` budget skips a company whose runs today already
exceed its hours, and Phase 4's allocation score shifts free slots toward companies with traction
([operating-model.md](operating-model.md)).

## Context discipline

Today: the system prompt carries the charter cut at 3,500 characters, 15 pinned plus 14 recent memory rows, the
team and the latest metric per name; the user prompt adds up to 40 file names. `agents._trim` keeps the
transcript under `num_ctx` times 2.6 characters by dropping stored thinking and cutting old tool results to
700 characters. `read_file` returns 12,000-character sections with an `offset`; `fetch_url` returns up to 9,000
characters of page text; every tool result is capped at 7,000 characters. `runs.tokens` records prompt plus
output tokens per run, but nothing shows how close a run came to the window.

Planned:

- **Digest instead of charter (Phase 2).** `brain.digest(company)` is at most 6,000 characters (about 1,500
  tokens): strategy lines, active goals with current versus target and days left, the latest metric per name
  with its source, open decisions due for review, running experiments, the newest 8 lessons and contact counts
  by stage. It replaces the charter in `system_prompt`; the charter stays in `docs/charter.md` and is listed
  under Files. `brain_lookup(section, query)` fetches the rest on demand.
- **Compression instead of truncation (Phase 3).** When `_trim` must shrink an old tool result, the `summary`
  job rewrites it into a short summary that keeps file paths, URLs, numbers and errors, instead of cutting it at
  700 characters. Long documents keep being read in sections through `read_file` with `offset`.
- **Context use shown in Settings (Phase 3).** `runs` gets `max_prompt_tokens`, the largest `prompt_eval_count`
  seen in the run. Settings shows context use per run as the p50 and p95 of the last 50 runs against `num_ctx`,
  with a warning when p95 is above 85% of the window: that is the sign a model's window is too small for the
  work, or that the digest or tool results need tightening.
