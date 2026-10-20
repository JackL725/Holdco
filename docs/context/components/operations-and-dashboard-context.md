# Operations and dashboard

## Purpose
Keeps every company working around the clock and keeps the owner informed and in control: the scheduler, the
watchdog, the Doctor page, phone notifications, backups, the HTTP server and the dashboard.

## Key files
- `holdco/scheduler.py`: `Scheduler.tick()` (see system-design.md), `_routines()` (preset routines, `at_hour` in
  local time, `requires` imap/smtp/no_analyst), `_queue()` and dispatch classes (follow-ups, due, p1, normal,
  routines, research in quiet hours), `_housekeeping()`, `recover()` (after a restart: interrupted runs back to the
  queue, leases cleared, a crashed bench cleaned), `_local_now()` (patchable; `HOLDCO_TEST_LOCAL_NOW`).
- `holdco/watchdog.py` (Phase 0): patterns `task_failed_twice`, `repeated_error`, `task_bouncing`,
  `connector_down`, `stalled`, `founding_partial`, each one question with options and a recommendation;
  `resolve()` applies the answer; working notes (`note_progress`) and retry-differently guidance.
- `holdco/doctor.py` (Phase 0): health rows (model server, model fit, web search, connectors, sandbox, disk,
  backups, recent errors, search index) with fixes; the founding preflight; `GET /api/doctor`.
- `holdco/notify.py` (Phase 0): Telegram bot (long polling, inline buttons answering through `actions.decide`,
  commands `/status`, `/stop`, `/resume`), ntfy (notify only), one sender thread, briefs and decisions rendering.
- `holdco/backup.py`: nightly zip of the database and company folders, newest `backup_keep` kept.
- `holdco/server.py`: `Handler` (auth, CSRF header, static files, previews, `/mcp`, `/api/` dispatch through
  `routing.ROUTES`), core API routes (state, settings, companies, agents, tasks, runs, chat, memory, metrics,
  emails, approvals, files), `api_company_patch` (setting coercion). Feature modules add their own routes.
- `holdco/routing.py`: the route registry and `HTTPError`. `holdco/bus.py`: events table and Server-Sent Events.
- `holdco/static/app.js`: the dashboard (views: Portfolio; company tabs Overview, Chat, Tasks, Brain, Team, Thinking,
  Files, Context, Memory, Email, Charter, Settings, listed in `TABS`; Inbox; Doctor; Model scores; global Settings),
  `esc()`/`md()` escaping.

## Dependencies
- Everything: the scheduler and server are the entry points that call into the other components.

## Integration points
- `run.py` starts the server, the scheduler thread and the poller; `HOLDCO_*` environment variables configure port,
  host, password, data folder, tick and routine intervals and test switches.
- Live updates: `bus.notify(topic, company_id=...)` makes open dashboards refetch.

## Patterns and rules
- Nothing in `tick()` may wait on a model or a slow network call; use a background thread.
- Every phone message that needs an answer has buttons that resolve the row through `actions.decide`.
- Bench companies (status `bench`) are excluded from every owner surface, count and notification.
- Dashboard strings from the API are escaped with `esc()`; model-written Markdown goes through `md()`, whose link
  rules forbid quotes, angle brackets and parentheses in addresses.
