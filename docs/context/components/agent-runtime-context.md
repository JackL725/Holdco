# Agent runtime context

## Purpose
Runs one agent work session: builds the prompt, lets the model call tools until it finishes, records everything,
and moves the task or the founding forward. Also owns presets (the 19 hireable roles) and tool belts.

## Key files
- `holdco/agents.py`: `run(agent_id, kind, task_id, text, ...)` (the loop), `start()` (a thread per session),
  `system_prompt()`, `task_prompt()`, `cycle_prompt()`, `founding_prompt()`, `_trim()` (context budget, small-model
  summaries of old results), `_finalize()` (run row, task status, notes, leases, budgets, founding hand-over),
  `send_chat()`, `running_count()`.
- `holdco/llm.py`: `chat()` for Ollama `/api/chat` or OpenAI-compatible `/chat/completions`; native tool calls or
  prompt-based JSON shaped by a schema (`tool_call_schema`); recovery of text and wrapped calls (`unwrap_call`,
  `_text_tool_call`); `Reply` (content, tool_calls, tokens, prompt_tokens, eval_count, eval_seconds, wall_seconds);
  `background_slot()`/`chat_slot()`; `pick_model(job)` and `run_job()` for small-model jobs.
- `holdco/tools.py`: `Ctx` (the session state every tool receives), `tool()` decorator and `REGISTRY`, `S()`/`I()`
  schema helpers (enums, ranges), `clean_args`/`_coerce` (repair messages), `resolve()` (`ALIASES`, `ARG_RENAMES`),
  `execute()`, the core tools (files, tasks, memory, owner messages, outbound, `finish`), and at the bottom the
  imports of feature modules that register more tools.
- `holdco/roles.py`: `tools_for(agent, kind)`: the preset belt plus run-kind extras (`CHAT_EXTRA`, `TASK_EXTRA`,
  `CONTEXT_EXTRA` = search, grep, edit_file, company_status, find_tool, `RESEARCH_EXTRA`); founding stage belts;
  `add_agent()`.
- `holdco/presets.py`: `PRESETS` (19 roles: ceo, engineering, marketing, sales, research, support, qa, analyst,
  seo, design, social, ops, product, discovery, lifecycle, partnerships, success, legal, ads), `R()` routines with
  `at_hour`, `TEMPLATES` (teams), `render()` (the role prompt).

## Dependencies
- Ground truth and context (facts block, changes, waiting, claims checks, search, memory, files index).
- Organization (brain digest, task contracts, rhythm data, budgets), safety (taint wrapper, verification gate).
- `db`, `bus` (events and live updates), `workspace` (paths), `watchdog` (working notes, failures).

## Integration points
- The scheduler and the dashboard start sessions (`agents.start`); chat comes from `POST /api/companies/<id>/messages`.
- Tools reach the outside world only through `actions.gate` (see safety-and-approvals).
- Events: `bus.emit(company_id, kind, message, agent_id, data)` feeds the Thinking and Feed views.

## Patterns and rules
- A run kind decides the belt and the step budget: founding stages (`planning.STAGE_STEPS`), cycle at least 18,
  chat at most 8, task `contracts.max_steps_for` (owner-set budgets only).
- `finish` checks run in a fixed order and each pushes back once (see system-design.md). When the session runs out
  of steps right after a refused finish, the handoff is kept as `partial`.
- Belts: executors at most 18 tools, the CEO at most 24 (`test_unit.TestPresets.test_integrity`). New tools must
  fit; outside services load on demand through `find_tool`.
- Unknown arguments are dropped silently unless they are a near miss of a declared argument; `null`, empty values
  and `0` for an optional ranged integer mean "not given".
- Text the model reads is plain English and actionable; never return a raw traceback.
