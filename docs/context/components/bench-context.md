# Holdco Bench

## Purpose
Scores a model on Holdco's own work (Phase 3): 33 scripted scenarios, each a seeded company and one agent session
(or one small-model job) against mocks, with assertions on what happened in the database and on disk. It also runs
in CI against the scripted model, so a prompt or preset change that breaks a scenario fails the build.

## Key files
- `holdco/bench/scenarios.py`: `Scenario` (name, purpose, job, seed, run, mocks, assertions, `lazy_fails`,
  `timeout_s`) and `SCENARIOS` (found from an idea, search blocked, research cites sources, fix a page failing QA,
  tool error recovery, no repeated failed call, respect approval, compliant cold email, kill switch, deliverable
  push-back, working notes retry, escalation after two failures, plan top down, brain proposal routed, dependency
  declared, file lease conflict, evening brief from data, weekly review, decision with recommendation, payment
  link, MCP tool via find_tool, connector down avoided, taint blocks publish, prompt injection ignored, tool call
  repair, unknown tool alias, support reply, triage, summary, memory dedupe, chat creates task, out of steps scope
  down, budget respected).
- `holdco/bench/runner.py`: `run_all(model, only, settings, progress)`, `check(assertion, state)`, scores
  (pass rate, tool-call validity, tokens per second from `eval_count`/`eval_seconds`, steps), per-scenario traces
  (what the model did, capped), the `bench_runs` row; bench companies have status `bench` and are deleted after.
- `holdco/bench/mocks.py`: the shared fake outside world (Stripe, Telegram, MCP stdio and HTTP, search, fetch,
  scripted `browser_check`); `install(spec)` patches per bench thread and `remove()` restores.
- `holdco/bench/__main__.py`: `python -m holdco.bench --model M [--only ...] [--json] [--data DIR] [--base-url URL]`
  (a temporary data folder and a free port unless told otherwise).
- `holdco/bench/__init__.py`: `RUNNING` (the scheduler pauses dispatch), routes `POST /api/bench/run`,
  `GET /api/bench/status`, `GET /api/bench/results[/<id>]`.
- `tests/fake_ollama.py`: `BENCH_SCRIPTS` (the right move per scenario) and `lazy` mode (the wrong move);
  `tests/test_phase3_bench.py`: 33 of 33 with the scripted model, and lazy mode fails exactly the `lazy_fails` set.

## Dependencies
- `agents.run` (synchronous sessions), `watchdog.tick`, `llm.pick_model` for jobs, the verification gate's shared
  web-page rule (`verify.judge_browser_check`).

## Integration points
- Settings "Test this model" and the Model scores page (`#/bench`) call the routes; the CLI writes to its own data
  folder.

## Patterns and rules
- An assertion must be able to fail: every `lazy_fails` scenario must fail in lazy mode.
- Change a scenario only when it is unfair to a careful operator with the same prompt, tools and mocks; never to
  make one model pass. Real-model scores vary between runs by several scenarios; compare patterns, not one run.
- A bench never touches the owner's companies: thread-local patches, its own companies, cleanup in `finally`.
