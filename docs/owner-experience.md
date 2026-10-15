# Owner experience

After Phase 4 the owner runs a portfolio in about 15 minutes a day, mostly from the phone: read one brief in the morning,
answer the decisions and approvals that arrived as buttons, and open the Doctor page only when its status dot
is not green. This document describes each piece of that: the daily brief and weekly board memo, the three
kinds of inbox items and how they are answered from the dashboard and from Telegram, the Doctor page and the
founding preflight, the kill switch, phone notifications, the Portfolio view with capital allocation, guided
connector setup, and backups. For each piece it states what the code does today and what each phase adds. The
agents' side of the loop (brain, goals, rhythms, verification, budgets) is in operating-model.md.

Status: design; built across Phase 0 (Doctor, kill switch, Telegram, backups: phases/phase-0-stabilize.md),
Phase 1 (connector setup cards: phases/phase-1-connect.md), Phase 2 (briefs and decisions:
phases/phase-2-organize.md), Phase 3 (trust ladder decisions: phases/phase-3-verify.md) and Phase 4 (portfolio
allocation: phases/phase-4-scale.md).

## Summary: 15 minutes a day, mostly from the phone

Today the owner's whole view is the dashboard: the Portfolio page (company rows with agents working, open
tasks, done today, requests waiting, and a live feed), the Inbox with its badge, Chat with the CEO, the Numbers
panel on each Overview, and the Thinking tab. Nothing reaches the owner away from the PC unless they expose the
dashboard on their network (`HOLDCO_HOST`, `HOLDCO_PASSWORD`, a private network such as Tailscale). Failures
show only as red text in the feed.

Planned, the day looks like this:

| When | What arrives | Where | Minutes |
|---|---|---|---|
| Morning | The daily brief per company and one for the portfolio, with every decision waiting listed under it with its recommendation | Telegram; pinned on the Portfolio page; the company Overview | 5 |
| During the day | Approvals as agents queue them; escalations within one scheduler tick of the problem; connector-down alerts once a day | Telegram buttons; the Inbox | 5 |
| Evening, optional | Portfolio numbers, the Doctor dot, the Thinking tab if something looks wrong | Dashboard | 5 |
| Weekly | The board memo: goal progress, experiment verdicts, decisions reviewed, tasks cancelled | Telegram; Portfolio | 10 |
| Monthly | A keep, change or kill decision per company, and the portfolio review | Telegram buttons; the Inbox | 10 |

The Phase 4 gate is exactly this: a week on the daily brief and the phone, with nothing missed.

## Daily brief and weekly board memo

Today: the CEO's "Morning report" routine (every 24 hours, first after 20 hours) posts under 200 words into the
company's Chat tab with `report_to_owner`; the analyst's "Weekly KPI report" goes to `ops/kpi-report.md`; the
CEO's "Weekly review" goes to `docs/weekly-review.md`. All are written by the model from what it can see, with
numbers typed in by agents (`record_metric`), and none leaves the dashboard.

Planned (Phase 2, `rhythms.py`): code gathers the numbers, the model writes the prose from those numbers only,
and the result is stored in `briefs(id, company_id, kind, body, data, created_at)`.

| Section of the daily brief | Computed by code (`data` JSON) | Written by the model (`body`) |
|---|---|---|
| What moved | Every metric with a connector source: latest value, change vs yesterday and vs 7 days ago; anomalies (more than 50% change on a metric with at least 7 days of baseline) | One line per metric that moved, with the real numbers; anomalies called out |
| What shipped | Tasks done in the last 24 hours, with deliverable paths and gate results | A short list with file paths or URLs |
| What is stuck | Blocked tasks, open escalations, connectors that are down | One line each with the cause |
| Decisions needed | Pending decisions and questions, each with its recommended option and deadline | The question and the recommendation, nothing more; the buttons do the rest |

The weekly board memo (`kind = weekly`) adds goal progress (current vs target per goal, from connector-sourced
metrics), experiment verdicts (won, lost, inconclusive, with the metric values), decisions reviewed and their
outcomes, tasks cancelled for serving no goal, and budget use (GPU hours, emails, posts). The monthly strategy
check (`kind = monthly`) ends in a keep, change or kill decision. The Phase 4 portfolio review (`kind =
portfolio`) covers every company in one memo.

Who writes what: the evening brief is written by the analyst agent, or by the CEO when the team has no analyst;
the weekly memo by the CEO using the analyst's data; the monthly check by the CEO. A number that is not in
`data` may not appear in `body`; the Phase 2 gate tests this with fake Stripe metrics.

Where it appears: one Telegram message per company brief and one for the portfolio summary
(`notify.send(kind=brief)`), the latest daily brief pinned at the top of the Portfolio page, and the company's
latest brief on its Overview. Older briefs are listed from the Portfolio page. Briefs are not resent: identical
text is suppressed for an hour by `notify.send`.

## Decisions, approvals and questions

Three kinds of item reach the inbox. All are rows of the `approvals` table, so one page, one API route and one
Telegram poller handle them.

| Kind | `action` | Payload | Dashboard buttons | Telegram buttons | Since |
|---|---|---|---|---|---|
| Approval: "may I send this?" | send_email, social_post, deploy_site, http_post, run_code, spend_money; Phase 1 adds connector_call | The action's arguments; connector calls carry `{connector_id, tool, args, tag}` | Editable fields (`to`, `subject`, `body`, `content`, `code`), a note field, Reject, and an Approve button labelled for the action ("Approve and send", "Approve and post", "Approve and deploy", "Approve and run", "Approve, I'll buy it") | Approve, Reject (`a:<approval_id>:approve` or `a:<approval_id>:reject`) | today (dashboard), Phase 0 (Telegram) |
| Question: "I need something only you can give" | question | `{question}` from `ask_owner`; escalations add `options`, `recommended`, `context`, `pattern`, `task_id` | A free-text answer box; Phase 0 adds one button per option with the recommended one marked | One button per option (`q:<approval_id>:<option index>`) | today (free text), Phase 0 (options) |
| Decision: "which of these should we do?" | decision | `{question, options, recommended, deadline_at, reversible, context}` | Its own section above Approvals; one button per option, the recommended one highlighted; the deadline and whether it resolves itself | One button per option | Phase 2 |

The answer path is the same from both places:

1. Dashboard: the Inbox posts `POST /api/approvals/<id>` with `{decision: approve|reject|answer, payload,
   feedback, answer}`. Phone: the Telegram poller receives the callback and calls `actions.decide` with the
   same arguments, then answers the callback and edits the message to show the outcome.
2. `actions.decide` executes an approved action (status done or failed), records a rejection, or stores the
   answer. It writes a memory note, reopens a waiting or blocked task with the answer, creates a "Revise: ..."
   task when a rejection carries a note, and asks the CEO to re-plan within 3 minutes.
3. Phase 0 adds `watchdog.resolve(row, answer)` for escalations (skip, retry, pause, retry founding, continue).
   Phase 2 writes a `decisions` row for every answered decision (`decided_by` owner or deadline).
4. The outcome appears under "Recent decisions" on the Inbox page; a failed action (SMTP down, for example)
   gets a Retry button there (`POST /api/approvals/<id>/retry`).

Card details added by later phases: the risk tag of the tool (Phase 1); "asked because this session read
outside content" when taint forced the approval (Phase 3); "runs unsandboxed on this PC" on a `run_code` card
when Docker is absent (Phase 3); "Decided by deadline" on auto-resolved decisions (Phase 2). Phase 0 adds the
Audit log panel to the Inbox page: every executed outbound action with who proposed it, who approved it (`approved_by` is `owner` or
`policy:auto`) and the payload sent (`GET /api/audit?company_id=&limit=`), append-only.

Reversible decisions resolve themselves at `deadline_at` (48 hours by default) with the recommended option and
the owner is told; irreversible ones wait. The rules are in operating-model.md, "Decisions vs approvals".

## Doctor page

Today there is no health page. The empty Portfolio page checks the model server once (`GET /api/llm/models`),
Settings has "Test connection" (`POST /api/llm/test`, a tool-call ping), the company Settings tab has "Send
test" for email, and a founding that stops shows a banner with "Retry founding". A blocked search provider is
visible only as "Search failed" lines in the feed.

Planned (Phase 0, `doctor.py`): `doctor.run(deep=False)` returns one row per check, cached in `doctor.LAST`,
refreshed hourly by the scheduler and on demand. Each row is `{key, label, status: ok|warn|bad, detail, fix,
checked_at}`; `fix` is `{kind: link|action|text, label, href|action}` or empty. Connector login rows (SMTP,
IMAP) are rechecked hourly until they have been ok for 24 h, then every 6 h, so consumer mail providers are not
hit with hourly logins; `Run checks` rechecks everything.

| Row (`key`) | ok | warn | bad | Fix |
|---|---|---|---|---|
| model_server | `GET /api/tags` on `llm_base_url` answers | | Unreachable or an error | link `#/settings` ("Model settings"); `detail` says how to start Ollama (Windows: the tray; Linux: `systemctl status ollama`) |
| model | `default_model` is installed | | Not installed | text: `ollama pull <model>` (or pick an installed one in Settings) |
| gpu_fit | Every loaded model (Ollama `/api/ps`) has `size_vram` at least `size`; or unknown, with a note | A loaded model has `size_vram` below `size`: "spilling into RAM" (nvidia-smi output in `detail` when present) | | text naming the README section "Getting the most out of an RTX 5090" (lower "Agents working at once" or the context window) |
| search | The last search answered; deep: a live query through the chain succeeded | At least one provider is cooling down after a block, another is available | Every provider failed or is cooling down (from `web.PROVIDER_STATUS`) | link to Settings: add a Brave key or a SearXNG address |
| `conn:<cid>:smtp`, `conn:<cid>:imap`, `conn:<cid>:netlify`, `conn:<cid>:webhook` (one per configured connector per company) | SMTP login, IMAP login, Netlify token (`GET /api/v1/user`), webhook URL syntax | | The check fails; `detail` carries the error | link to the company's Settings tab |
| `telegram`, `ntfy` (global) | Telegram `getMe` answers; ntfy topic set and the server parses as a URL | Telegram token set but no chat id | The request fails; the ntfy server is malformed | link to Settings |
| vault (Phase 1) | The vault backend is readable | | An unreadable vault file, or a secret stored by another machine's backend | re-enter the key |
| disk | At least 5 GB free in the data folder | Below 5 GB | Below 1 GB | text: free space on the drive holding `DATA_DIR` |
| backup | Last backup younger than 26 h | Younger than 72 h | Older, or never | action: backup (`POST /api/backup`) |
| errors_24h | No `error` events in 24 h | 1 to 9 | 10 or more; `detail` shows the top 3 messages | link `#/` labelled "Open the live feed" (the Portfolio page's feed shows error events) |
| connector:<id> (Phase 1) | The instance's health check passes | | It fails on this check (the watchdog escalates on 2 consecutive failures) | link to the connector's setup card |
| sandbox (Phase 3) | Docker or Podman found | "Code sandbox: not available" | | text: install Docker, or keep `run_code` on Ask me first |

API: `GET /api/doctor` returns the cached rows and a summary `{ok, warn, bad}`; `POST /api/doctor/run` refreshes
now, and with `deep: true` also runs the model tool-call ping and a live search. The page is `#/doctor`, listed
in the sidebar with a status dot whose colour is the worst status in the summary (red for any bad row, yellow
for any warn, green otherwise). The dot refreshes with the hourly check and after a manual run; `/api/state` carries the same summary under
`doctor`, so the sidebar needs no extra request.

### Founding preflight and the 409

Today `POST /api/companies` creates the company and starts founding at once; if the model server is down the
founding run errors and the owner finds out from the Overview banner.

Planned: `doctor.preflight()` runs first and returns `{"ok", "checks", "soft"}`. The hard checks are
`model_server` and `model`; `search` is soft (from the cache when it is under an hour old, else a quick live
test). When a hard check fails the route returns `409` with `{"error": "Fix this before founding: <label>:
<detail>; ...", "checks": [rows]}` unless the body carries `force: true`. The New company page shows
the failed checks with their fixes and a "Found anyway" button that resends with `force`. A soft failure founds
anyway, and the founding prompt tells the CEO that search is down: skip research, note the gap, ask the owner.

## Kill switch

Today the sidebar's Autopilot toggle sets `scheduler_enabled` (no new dispatch; running agents finish their
session), and each company has Pause (its agents stop at the next step). Neither freezes the outbound queue:
an approval can still be approved and executed, and `auto` policies still run.

Planned (Phase 0): setting `kill_switch`, toggled by `POST /api/kill {"on": true|false}` and carried in
`/api/state`. When on:

| Part | Behaviour |
|---|---|
| Scheduler | Dispatches nothing (no cycles, no tasks, no routines) |
| Running agents | Stop at the next step boundary with run status `stopped` and the error text "Kill switch: the owner stopped all agents."; their tasks go back to todo |
| `actions.gate` | Queues `auto` actions for approval instead of executing them, with the note "Kill switch is on" |
| `actions.decide` | Approving raises `ValueError("The kill switch is on. Turn it off to run approvals.")`; rejecting and answering still work |
| Telegram poller | Keeps running so `/resume` works from the phone |
| Dashboard | A red "Stop everything" button in the sidebar; while on, a red banner on every page with a Resume button |

Turning it on does not change `scheduler_enabled`, so resuming returns to the previous state. From the phone:
`/stop` turns it on, `/resume` turns it off, and both send a "kill switch toggled" notification so the owner
sees it happened.

## Phone notifications

Planned (Phase 0, `notify.py`). Telegram carries messages and buttons; ntfy carries messages only.

### Setup

1. In Telegram, open BotFather and create a bot; it gives a token. Paste it into Settings, "Phone
   notifications", Bot token (`telegram_bot_token`). It is a secret setting: masked in the API, moved to the vault
   in Phase 1.
2. Send the new bot any message so it may reply to you, then find your numeric chat id from the bot's updates
   (Telegram's `getUpdates` method; verify against the vendor docs before connecting) and paste it into Chat id
   (`telegram_chat_id`). Messages and button presses from any other chat id are ignored.
3. Press "Send test message" (`POST /api/notify/test`). A message with a test button should arrive within a few
   seconds; pressing it confirms the callback path.
4. Pick `notify_level`: `important` (default: requests, escalations, errors and briefs) or `all` (also one line
   per finished agent session).

`telegram_api_base` defaults to `https://api.telegram.org` and exists so tests can point the poller at
`tests/fake_telegram.py`. For ntfy, set `ntfy_topic` and, if self-hosted, `ntfy_server` (default
`https://ntfy.sh`). Anyone who knows a topic name on the public server can read it, so use a long random name
or a private server.

### What arrives and when

| Event | Message | Buttons | Level | Since |
|---|---|---|---|---|
| New approval request | The card summary and the payload's key fields (recipient and subject, post text, code's first line, purchase item and cost) | Approve, Reject | important | Phase 0 |
| New question or escalation | The question and its context, the recommended option marked | One per option | important | Phase 0 |
| Founding finished | Company name and a one-line result; "ended partial" comes as an escalation | none | important | Phase 0 |
| Kill switch toggled | On or off, and by whom (dashboard or phone) | none | important | Phase 0 |
| Error event | The message, once per distinct message per hour | none | important | Phase 0 |
| Run finished | One line per agent session: who, what, how it ended | none | all | Phase 0 |
| Connector down | Which connector, which company, the error; once per connector per day | none | important | Phase 1 |
| Daily brief, weekly memo, monthly check | The brief body | One per decision listed in it | important | Phase 2 |
| Decision | Question, options, recommendation, deadline | One per option | important | Phase 2 |
| Decided by deadline | Which decision resolved itself and with what | none | important | Phase 2 |
| Trust promotion suggested | "Let agents <action> on their own, up to N a day?" | One per option: Yes, up to N a day (recommended); Yes, always; No, keep asking | important | Phase 3 |

`notify.send(text, buttons=None, kind)` sends to Telegram (HTML parse mode, inline keyboard) and to ntfy (plain
text, no buttons) when each is configured. Identical text is not resent within an hour.

### Callback handling

The scheduler starts one long-polling thread when both the token and the chat id are set (`getUpdates` with a
25-second timeout; it restarts after errors with backoff). No port is opened. Callback data is
`a:<approval_id>:approve|reject` for approvals and `q:<approval_id>:<option index>` for questions and
decisions. The thread calls `actions.decide`, then answers the callback query and edits the original message
to show the outcome ("Approved and sent", "Rejected", "Answered: Skip this task", or the error, for example
when the kill switch is on). A button pressed twice gets "already decided". Text commands: `/status` (portfolio
summary: companies, agents working, requests waiting), `/stop` (kill switch on), `/resume` (kill switch off).

### What is rate-limited

| What | Limit | Where |
|---|---|---|
| Any identical message | Not resent within 1 h | `notify.send` |
| Error events | One per distinct message per hour | `notify` error hook |
| Search outage | One error event per 30 minutes while every provider is failing | `web_search` tool |
| Connector down | Once per connector per day | connector health (Phase 1) and the `connector_down` pattern |
| Escalations | A pending question with the same pattern key is not duplicated; a resolved one is not recreated within 6 h | `watchdog` |
| Daily email cap | `daily_email_limit` per company, counting pending approvals | `send_email` (today) |

## Portfolio view and capital allocation

Today the Portfolio page lists each non-archived company: name, status badge (Running, Founding, Paused),
stage, one-liner, agents working out of total, open tasks, done today, "N requests for you", and when it was
last active; a live feed sits beside the list. Phase 2 pins the latest daily brief above the list.

Planned (Phase 4, `portfolio.py`): the list becomes a table with these columns per company.

| Column | Source |
|---|---|
| Goal progress | `goals.current` vs `target` for active goals, as a fraction and days left |
| Revenue | Stripe metrics (`stripe.revenue_usd_30d`, `stripe.mrr_usd`) |
| GPU hours this week | Sum of `runs` durations |
| Money spent | Approved `spend_money` purchases |
| Days since last real-world result | The newest of: a connector-sourced metric that changed, an inbound reply, a payment |
| Allocation score | Computed by `portfolio.py` from goal progress, revenue and days since the last real-world result (weights in `portfolio.WEIGHTS`); GPU hours and money spent are shown but not scored; shown with the share of concurrency it gives |

The allocation score drives the scheduler's share of `max_concurrent_runs`: companies with traction get first
pick of free slots, and no company starves (a minimum of one run per cycle). A monthly portfolio review (brief
`kind = portfolio`) recommends scale, hold or shut down per company and files it as a decision with those
three options; "shut down" is irreversible, so that decision never resolves itself.

## Guided connector setup

Today each connector is a group of fields on the company's Settings tab (SMTP and IMAP, a Netlify token and
site id, a webhook URL) and the search settings in global Settings (provider, SearXNG address, Brave key). Only
email has a test button. Values are stored as plain text in the database.

Planned (Phase 1, `connectors.py`): every connector has a definition with a setup card, and every instance
lives in the `connectors` table with its secrets in the vault. The Settings page (global scope) and each
company's Settings tab (company scope) render the cards.

| Card field | What it shows |
|---|---|
| Where to get the key | The vendor page, as a link (`docs_url`) and a sentence |
| Permissions to tick | Exactly which scopes to grant: a Stripe key with the Agent tag, a Porkbun key without purchase rights, a GitHub token with read-only toolsets for most roles |
| Fields | `[{key, label, secret, hint, placeholder}]`; secret fields are masked after save and never shown again |
| Test mode note | Which test or sandbox mode to start in, and how Holdco shows which mode is live |
| What agents can do | The tools this connector exposes, each with its risk tag (read, write, publish, contact, spend, irreversible) and the approval policy that tag maps to |
| Test button | `POST /api/connectors/<id>/test` runs `health()` and shows the result; saving (`POST /api/connectors`) runs it too |
| Status | The latest health result and time; a failing instance also appears on the Doctor page |

Test modes come first. The table shows the mode each shipped connector starts in; the owner flips it live per
company by changing the key or the setting named.

| Connector | Scope | Starts in | How to go live |
|---|---|---|---|
| stripe | company | Test mode: the mode is derived from the key prefix and shown on the card | Enter a live key with the Agent tag |
| resend | company | Draft-only in effect: with `send_email` on Ask me first every email waits for approval | Set `email_provider` to resend and promote the policy later through the trust ladder |
| playwright | global | Headless and isolated; nothing to flip | n/a |
| buffer | company | No test mode offered; keep `social_post` on Ask me first | Promote through the trust ladder |
| brave | global | n/a (read-only search) | n/a |
| porkbun (Phase 4) | global (purchases are attributed to the requesting company) | Sandbox keys (prefix `pk1_sb_`) and dryRun, both on by default; registering a domain is spend plus irreversible and always asks | Enter production keys and turn dryRun off |
| github (Phase 4) | global | Read-only toolset for every role except engineering | Grant engineering write toolsets |

An MCP tool Holdco has not classified defaults to "Ask me first" (connector_publish). Details of each
connector are in connectors.md; the policies the tags map to are in safety.md.

## Backups and restore

Today: the README says to copy the `data/` folder by hand. Nothing is automatic.

Planned (Phase 0, `backup.py`):

| Item | Value |
|---|---|
| Where | `data/backups/holdco-YYYYMMDD-HHMMSS.zip` |
| Contents | `holdco.db` (copied with sqlite3's `Connection.backup` into a temp file first, so WAL is consistent), `companies/` without `.runs`, `.trash`, `__pycache__` and `node_modules`, and the vault file from Phase 1 |
| How many | The newest `backup_keep` (default 14); older ones are deleted after a successful run |
| When | The scheduler runs it in a thread when the last backup is older than 24 h; on demand with `POST /api/backup` |
| Listing | `GET /api/backups` and the Doctor `backup` row (ok under 26 h, warn under 72 h, bad otherwise or never) |

Restore by hand:

1. Stop Holdco (close the window it runs in, or stop the autostart service).
2. Open the zip and copy `holdco.db` and `companies/` over the data folder (`data/` next to the README, or
   `HOLDCO_DATA`). Keep a copy of the current folder first.
3. Start Holdco. The scheduler's `recover()` puts interrupted work back in the queue.

On another machine, secrets do not come back: the vault records which backend protected each secret, so a
restored vault from a different machine fails loudly rather than silently, and the Doctor page shows each
affected connector as bad with "re-enter the key" as the fix.
