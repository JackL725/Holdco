# Phase 0.5: Ground truth and context

Status: built. Branch `claude/phase-0.5-context`, its own pull request, stacked on Phase 0. Covered by the suite (471 tests, no network or GPU) including a replay of the first run's failures, and re-run on the owner's RTX 5090 with gpt-oss:20b against Rooforyou's exact idea; the pull request has the before-and-after numbers. Differences from this design that the code settled: questions must name the decision and may not ask the owner for work the team can do; fetched pages are searchable (scope `pages`) and readable as `page:<url>`; founding stages must leave their deliverable; the charter source check covers only sections about the world.

This phase fixes what the first two real runs showed, and gives every agent easy access to everything Holdco
knows: its files, the owner's context files, memory, tasks, emails, decisions, past sessions and the real state
of the outside world. It comes before connectors because connecting Stripe and email to agents that invent
evidence would make the failures more expensive, not less.

## What the real runs showed

Rooforyou ran for 13 hours on gpt-oss:20b with the pre-Phase-0 code. A second company, CLI Weekly, ran on the
Phase 0 code. The full audit is in the conversation that produced this document; the numbers that matter:

| Rooforyou, first 13 hours | Result |
|---|---|
| Work sessions | 96 |
| Tokens | 9.6 million, 84% of them in CEO planning cycles |
| Planning cycles that ended "waiting on owner" | 35 |
| Questions to the owner, still pending | 38, mostly the same deploy-and-outreach question |
| Average files the CEO read per planning cycle | about 10 |
| Real-world results | no deploy, no email, no signup |
| Files that record events that never happened | 5: a deployment confirmation, a form test, a submission log, an email "sent" log, a recruitment log |
| Charter saved to the company record | no, it exists only as a file |

The single root cause behind most of it: **Holdco never shows agents what has really happened**, so they keep
their own state in files. The CEO's most re-read files were ones it had created to track state:
`owner_approval_status.txt`, `owner_approval_sent.txt`, `email_log.txt`, `netlify_deployment.md`. When an agent
cannot see that a deploy is still waiting for approval, a file saying "deployed" is as good as the truth to it,
and the next agent believes it. The CEO then told the owner the site was live.

The other findings, each with its fix in this document:

| Finding | Fix |
|---|---|
| Agents invent state and evidence in files | Facts block, records folder, claims gate, action-task contracts |
| The same question asked 38 times | One open question per topic, a cap, structured questions with options |
| The CEO re-plans every 10 minutes while everything waits on the owner | Change detector, backoff, quiet waiting, daily token budget |
| Founding spends its whole step budget on research and creates no tasks | Staged founding with a budget per stage |
| The charter written with `write_file` never reaches the company record | One charter path with validation |
| The charter misreads the business (no price, competitors or demand plan) | Business archetype playbooks, required charter sections, a critic pass |
| Research prices with no sources | Fetched-page cache, verified facts ledger, source check on research files |
| Placeholder analytics tag and a blank social image in production | Placeholder detection in checks, deploy gate |
| "Sent" emails to placeholder addresses, no postal address | Outreach checks before queueing |
| Planning notes dated in the future | Time in the facts block, future-date rule |
| A blocked page fetched through a third-party proxy | Relay proxies blocked |
| Agents call tools that do not exist (`search_file`, `search_text`) | A real search tool, grep, aliases |
| Large files written in one tool call trip the model server's parser | `edit_file` for small changes, sectioned writes |
| A link to a Substack the company does not have | Accounts list in the facts block, claims gate |

## Principles

1. **The system is the source of truth.** Facts about the world (deploys, emails sent, signups, payments,
   approvals, owner answers) come from Holdco's database, never from files agents write. Agents read them and
   cannot edit them.
2. **Context is layered.** A small always-on core in every prompt, everything else one tool call away through
   a single search. No agent should need to open ten files to learn what is going on.
3. **Claims are checked before they reach the owner.** Anything an agent tells the owner or saves as a
   deliverable that claims a real-world event is checked against the records.
4. **The owner's attention is scarce.** One open question per topic, each with options and a recommendation.
5. **Effort follows change.** No new information means no new planning.

## 1. Context system (`holdco/context.py`)

### 1.1 What every prompt contains

The system prompt is rebuilt from sections, each with a size cap, so it stays small and predictable. Sizes are in
characters; four characters are roughly one token.

| Section | Source | Cap |
|---|---|---|
| Identity and role | existing presets | unchanged |
| Owner's standing orders | company settings | 1,500 |
| Facts and capabilities | `truth.facts_section` (section 2.1) | 2,000 |
| Company core | charter fields: one-liner, customer, offer and price, North Star, riskiest assumption; full charter on demand | 1,500 |
| Playbook | `playbooks.section` for the company's archetype (section 4.3) | 1,200 |
| Owner context, pinned | context files the owner marked "always include" | 2,500 |
| Since your last session | `truth.changes_section` (section 2.3) | 1,200 |
| Waiting on the owner | pending questions and approvals with ids and age | 800 |
| Team | existing | unchanged |
| Memory | pinned notes plus the notes most relevant to the task, found by search, not just the newest | 1,800 |
| Context index | context files and key documents, one line each with a summary | 1,500 |
| Operating rules | existing, plus the new rules below | unchanged |

New operating rules, added for every run kind:
- "Facts about the outside world come from the Facts block and the records/ folder. Never write a file that
  records something as done unless the Facts say it happened."
- "To find anything, call `search` first. It covers files, the owner's context, memory, tasks, emails, chat,
  decisions and past sessions."
- "Change a file with `edit_file`. Rewrite a whole file only when most of it changes."

### 1.2 The owner's context library

Two folders hold context files the owner writes or drops in:
- `data/context/` for everything: who the owner is, their skills, budget, preferences, brand rules, things to
  avoid. Every company sees it.
- `data/companies/<slug>/context/` for one company: customer interviews, notes, price lists, competitor notes.

Text formats only: `.md`, `.txt`, `.csv`, `.json`, `.html`, `.xml`, `.yaml`, `.yml`. Other files are listed but
not indexed, with a note that they need converting to text. Agents can read and search the library but cannot
write to it: `write_file` and `edit_file` refuse paths under `context/` with
"ERROR: context/ is the owner's. Save your notes elsewhere, for example docs/." The owner manages it from a
Context tab on each company and a "Your context" page under Settings, with an "Always include" toggle per file.
Pinned files are included in every prompt up to the cap; when they exceed it, the prompt says which pinned files
were cut and that they can be read with `read_file`.

Settings: `pinned_context` (global setting, list of paths under `data/context/`) and `pinned_context` (company
setting, list of paths under the company's `context/`).

### 1.3 The index

One SQLite FTS5 table indexes everything an agent might need:

```
CREATE VIRTUAL TABLE search_index USING fts5(
    title, heading, body,
    company_id UNINDEXED, source UNINDEXED, ref UNINDEXED, line UNINDEXED, updated_at UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE TABLE index_state (company_id INTEGER, source TEXT, ref TEXT, stamp TEXT,
                          PRIMARY KEY (company_id, source, ref));
```

| `source` | `ref` | What is indexed |
|---|---|---|
| `file` | path in the company folder | chunks of every text file outside `.runs`, `.trash`, `.cache` |
| `context` | `context:<path>` or `global:<path>` | chunks of context library files |
| `memory` | memory id | the note |
| `task` | task id | title, description, acceptance, handoff, working notes |
| `email` | email id | from, to, subject, body |
| `chat` | message id | owner chat messages and agent replies |
| `approval` | approval id | summary, payload text, decision and outcome |
| `run` | run id | the session summary |
| `fact` | fact id | the claim, its quote and URL (section 5.2) |

Chunking: Markdown splits at headings, HTML is converted to text with its headings kept, code and other text
split into 60-line windows, CSV keeps the header and the first 50 rows. Each chunk records its first line
number so search results point at `path:line`.

Indexing is incremental and cheap. Files are re-indexed when their size or modification time changes (the
`stamp`), checked at the start of every run and after every `write_file` or `edit_file`. Database rows are
indexed by id above the last indexed id per source, plus updates for rows that change (tasks, approvals). A full
rebuild runs on startup when the index is missing. The index never blocks a run: a failure is logged and search
falls back to a plain `LIKE` scan.

Optional meaning-based search: when the global setting `embedding_model` names an installed Ollama embedding model
(for example `nomic-embed-text`), chunks also get vectors in `search_vectors(rowid, model, vector)` through
Ollama's `/api/embed`, and search merges both rankings with reciprocal rank fusion. Off by default; Phase 3's
model routing builds on it.

### 1.4 Tools

| Tool | What it does | Risk tag |
|---|---|---|
| `search(query, scope="all", limit=8)` | Ranked snippets from the index: `path:line`, heading, snippet with the matching words marked. `scope` is all, files, context, memory, tasks, emails, chat, decisions, sessions or facts. | read |
| `read_file(path, start_line=1, end_line=0, outline=false)` | Reads by line range (200 lines by default, with a pointer to the next range). `outline` returns the headings with line numbers instead of the text. The old `offset` argument still works. Reads `context:` and `global:` paths from the library. | read |
| `grep(pattern, path=".", regex=false)` | Literal or regular-expression search over the company folder, up to 50 matches with line numbers. | read |
| `edit_file(path, find, replace, count=1)` | Exact text replacement. Refuses when `find` is missing or matches more often than `count`, and returns the changed lines. | write |
| `list_files(path)` | Unchanged, plus each file's one-line summary from the index. | read |
| `company_status()` | The full facts and records (section 2), for when the prompt's compact version is not enough. | read |

`recall` stays for compatibility and becomes `search(scope="memory")`. Aliases map the names models invent:
`search_file`, `search_files`, `search_text`, `find_in_files`, `grep_files` to `search`; `cat`, `open_file`,
`view_file` to `read_file`; `replace`, `patch`, `str_replace` to `edit_file`. An unknown tool name answers with
the closest real names.

Every role gets `search`, `grep`, `edit_file` and `company_status` in addition to its belt, so the belts grow by
four tools. That stays inside the small-belt rule because the four replace guessing.

### 1.5 Keeping context small during a session

- Long reads default to 200 lines and say where to continue.
- When the conversation nears the context budget, old tool results are replaced by one line that keeps the
  reference ("[read site/index.html lines 1 to 200, shortened to save room; read again if needed]") instead of
  the current cut at 700 characters.
- Files larger than 8,000 characters are written in sections: `write_file` answers with a reminder to use
  `append` or `edit_file` for the rest, which also avoids the model server's errors on very large tool calls.

## 2. Ground truth (`holdco/truth.py`)

### 2.1 The facts block

Generated from the database for every prompt, and shown to the owner on the company Overview as "What your agents
know", so both see the same thing:

```
# Facts (from Holdco's records; these override anything in files)
Now: Thursday 15 October 2026, 09:40 (Europe/London). Company founded 11 hours ago.
Website: local preview only. Not deployed. Deploy request #5 waiting for the owner since 06:12.
Email: not connected. You cannot send email until the owner adds a mailbox. Sent so far: 0.
Social posting: no webhook or account. Approved posts become a link for the owner.
Accounts this company has: none besides the local website. Do not link to or claim any other account
  (domain, Substack, analytics, payment page) until it appears here.
Money: $0 spent. Purchases always need the owner.
Waiting on the owner: 1 question (#1, deploy and outreach, 9 h), 1 approval (#5, deploy).
Signups, revenue, replies: none recorded from a real source.
Down right now: web search (all providers failed at 09:12).
```

Each line comes from a specific source: `site_url` and the audit log for the website, SMTP and IMAP settings and
the `emails` table for email, the approvals table for pending items, metrics with a non-manual source for
signups and revenue, the Doctor for what is down. The time uses the PC's local time zone.

### 2.2 The records folder

Holdco writes `records/` in each company folder from the database, at the start of every run and after every
outbound action or owner decision:

| File | Contents |
|---|---|
| `records/STATUS.md` | the full facts block plus pending items |
| `records/outbound.md` | every outbound request: what, who asked, status (queued, approved, sent, failed, rejected), when, result |
| `records/emails.md` | the last 100 emails sent and received |
| `records/owner.md` | every question to the owner and every answer, newest first |

Agents read these like any file. `write_file` and `edit_file` refuse `records/` with
"ERROR: records/ is written by Holdco from what really happened. Write plans and notes elsewhere."

`write_file` also refuses new files whose names look like event logs or confirmations
(`log`, `logs`, `confirmation`, `receipt`, `sent`, `submission`, `submissions` as the name or a `_`/`-` separated
part of it, in a text format) with "ERROR: Holdco keeps the real record of what happened in records/. If this is
a plan, name it as a plan, for example docs/outreach-plan.md." Existing files with such names can still be
updated, and the owner can write anything from the dashboard.

### 2.3 Since your last session

For each agent, the changes since its previous run, newest first, capped: tasks created, finished, blocked or
cancelled; files changed and by whom; owner answers and decisions; new inbound email; connectors that went down
or came back; new facts. For a planning cycle where nothing changed, the section says
"Nothing has changed since your last planning cycle." (see section 3.3).

### 2.4 The claims gate

`truth.check_claims(company, text)` finds sentences that state a real-world event and checks each against the
records. It runs on `write_file` and `edit_file` content for files outside `code/` and `site/`, on
`report_to_owner`, `ask_owner`, and on `finish` summaries and evidence.

| Claim family | Example words | Supported only when |
|---|---|---|
| Deployed or live | deployed, is live, published the site, live at | a `deploy_site` action ran successfully (audit log) and the URL matches `site_url` |
| Email sent | sent, emailed, reached out to, delivered | `emails` has outgoing rows; a stated count must not exceed them |
| Signups or recruits | signed up, joined, subscribed, recruited, registered for | signup metrics from a real source exist (none before Phase 1 forms) |
| Form tested live | submitted, test submission, form works on the live site | the site is deployed and a submission is recorded by a connector |
| Posted | posted, tweeted, shared on | a `social_post` action ran successfully |
| Paid or purchased | paid, purchased, bought, registered the domain, formed the LLC | an approved purchase exists |
| Replies | replied, responded, booked a call | inbound email from that address exists |

Sentences that are clearly not claims are ignored: those with "draft", "plan", "planned", "will", "once",
"after approval", "pending", "queued", "waiting", "if", "proposed", "to do", "not yet", "should", "next step",
or a question mark. The check is deliberately conservative: it only fires on the families above, which are the
ones the real run faked.

When a claim is unsupported:
- `write_file` and `edit_file` refuse the change and list each sentence with what the records say, for example:
  "Line 3 says the site is deployed at https://rooforyou.netlify.app, but no deploy has run (request #5 is waiting
  for the owner). Write 'ready to deploy, waiting for approval' instead."
- `report_to_owner` and `ask_owner` refuse to send, with the same list.
- `finish` answers "Not finished yet" once with the list; the second time the run ends `partial` with the
  unsupported claims named in its open issues.

Every refusal is counted in an event of kind `claims` so the Bench (Phase 3) can score how often a model tries.

### 2.5 Action tasks

`create_task` infers a `kind` from the title, description and deliverable (the agent can also pass it): `outreach`,
`deploy`, `post`, `purchase`, `ask`, `research`, `build`, `write`. For the action kinds (outreach, deploy, post,
purchase, ask), the deliverable is the action, not a file:
- Calling the matching tool in the task queues the request; the task ends `waiting` and is reopened when the owner
  decides, as approvals already do.
- `finish(status="done")` succeeds only when the matching action ran (records), or ends `waiting` when it is
  queued. A file such as `docs/email_log.txt` never satisfies an action task.
- A task created with an action kind and no way to do the action today (email not connected) is answered with the
  facts line ("Email is not connected; this task cannot run yet. Ask the owner for a mailbox once, or plan work
  that does not need email.") instead of being created.

### 2.6 Placeholders

`checks.find_placeholders(text, is_html)` finds: analytics ids like `G-PLACEHOLDER`, `G-XXXXXXX` and `UA-XXXX`;
`placeholder@`, `example@`, and `example.com`, `example.org` or `example.net` addresses and links; "lorem ipsum";
`TODO`, `TBD`, `FIXME`; template tokens such as `{{name}}`, `[Company Name]`, `YOUR_`; social images that are
tiny data URIs; links to `#` used as real links. `audit_page` reports each as a blocker and `review_text` as an
issue. `deploy_site` refuses while any blocker remains, with the list.

### 2.7 Outreach checks before queueing

`send_email` checks before the request reaches the owner:
- The recipient is a real-looking address: not a placeholder, not an `example.*` domain, not a role address the
  owner blocked, not on the suppression list (addresses that replied "stop", "unsubscribe" or "remove me", across
  all companies, built from inbound email).
- The company has a sender name and a postal address (new company setting `postal_address`, required by CAN-SPAM
  for commercial email). Without it the tool answers that the owner must add it in Settings.
- The body ends with the company name, the postal address and an opt-out line; Holdco appends a standard footer
  when the agent left it out.
- The daily cap, as today.
Failures return "ERROR: email not queued:" with every reason.

### 2.8 Time

The facts block carries the date, time and time zone. `write_file` refuses file names containing a date after
today ("ERROR: that date is in the future; today is 15 October 2026"). The claims gate also treats a dated
statement of a past event with a future date as unsupported.

### 2.9 Web

`web.check_public_url` refuses relay services that fetch other sites on an agent's behalf: `r.jina.ai`,
`s.jina.ai`, `*.translate.goog`, `12ft.io`, `cors-anywhere` hosts, `allorigins.win`, `corsproxy.io`,
`thingproxy.freeboard.io`, kept in one list. The answer says why ("relays hide where requests go and break the
site's terms") and suggests the search snippet or another source. HTTP 403 results from `fetch_url` carry the same
suggestion.

## 3. Owner questions and planning effort (`holdco/planning.py`)

### 3.1 One open question per topic

`ask_owner(question, options="", recommended="", context="")`:
- `options` (one per line) and `recommended` render as buttons in the Inbox and on the phone, like the watchdog's
  questions. The tool description asks for options and a recommendation whenever there is a choice.
- Before queueing, the question is compared with every pending question and approval of the company. Topics are
  detected from keywords (deploy or publish, email or outreach, analytics, domain, money or budget, account or
  credentials, pricing, legal) and the rest by word overlap (Jaccard similarity of content words of 0.5 or more).
  A match answers "Already waiting on the owner as #1 (deploy and outreach, asked 9 hours ago). Do not ask again.
  Continue with work that does not need the answer, or finish with status 'waiting'." A pending approval counts
  too: a question about deploying while a `deploy_site` request waits answers "Your deploy request #5 is already in
  the owner's inbox."
- At most three questions from agents can be pending per company. A fourth is refused with the list of pending
  ones and the advice to fold it into one of them.
- When the owner answers, the answer is written to memory (as today) and to `records/owner.md`, and the topic is
  free again.

### 3.2 Waiting is a valid state

A task waiting on the owner is not dispatched until the owner acts (as today). The planning prompt shows what is
waiting and tells the CEO: "If nothing changed and the open work is waiting on the owner, finish at once with
status 'waiting' and a one-line note. Plan only work that does not need the answer."

### 3.3 Effort follows change

Each company has a change stamp: the highest id among meaningful events (owner chat, owner decisions, tasks
finished, blocked or cancelled, inbound email, owner file edits, connector status changes, facts recorded, metrics
from a real source) plus the counts of pending items. Stored in `companies.last_cycle_stamp`.
- The idle trigger (no task queued or running) fires only when the stamp changed since the last cycle.
- The due trigger (every `cycle_minutes`) also skips when the stamp is unchanged, and the interval doubles each
  time it skips, up to eight times (`companies.cycle_backoff`). Any change resets it.
- Owner actions (chat, answers, "Plan now") still trigger a cycle right away.
- When everything waits on the owner, the owner gets one "still waiting on you" line per day on the phone and in
  the brief instead of a planning cycle every few minutes.

### 3.4 Daily token budget

Company setting `daily_token_budget` (default 3,000,000; 0 turns it off). When a company's tokens for the day pass
it, the scheduler starts no background runs for it until midnight; owner chat and owner-started runs still work.
The Doctor shows the companies over budget and the owner gets one message. Rooforyou spent three times this in
13 hours, almost all of it re-planning.

## 4. Founding and planning quality

### 4.1 Staged founding

Founding becomes three short runs instead of one long one, tracked in the company setting `founding_stage`:

| Stage | Steps | Tools | Must produce |
|---|---|---|---|
| research | 12 | web_search, fetch_url, search, read_file, write_file, record_fact, note_progress, finish | `research/market.md` with sources, facts recorded |
| charter | 10 | read_file, search, update_charter, record_fact, finish | a charter that passes validation (4.2) |
| plan | 12 | read_file, search, create_task, ask_owner, report_to_owner, finish | 5 to 8 first tasks with deliverables, the welcome brief |

A critic run (4.4) sits between charter and plan. If research fails (search down), the charter stage runs with the
gaps noted. The company becomes active after the plan stage. A restart, the kill switch or an error resumes at the
stage it stopped in. The Phase 0 founding watchdog question applies per stage.

### 4.2 One charter path, validated

`update_charter` checks the charter has these sections with real content, and refuses with the list of what is
missing: One-liner; Customer; Problem; Offer and price (a price, or a range to test); How we reach customers;
Competitors (at least three named, with how we differ); Business model (who pays, for what, a unit-economics
estimate labelled as an estimate); Riskiest assumption (with a pass threshold); North Star metric; 30-day plan. It
saves the one-liner as the mission and moves the stage from idea to building. `write_file` or `edit_file` on
`docs/charter.md` goes through the same validation and saves to the company record, so there is one path whatever
tool the model picks.

### 4.3 Business archetype playbooks (`holdco/playbooks.py`)

A playbook per business type: lead generation or marketplace, newsletter or content, SaaS, services or agency,
digital products, e-commerce, local services. Each has how money flows, the riskiest assumptions that type
usually has, first-week experiments that touch real customers, the metrics that matter, common traps and
compliance notes. Example, lead generation: "Two sides. The business is producing customer demand for less than
providers pay per lead. Test both sides in week one: ask 10 providers what they pay per lead and to whom, and test
whether you can get customer inquiries at all. Trap: building the provider side first. Consent: never contact
consumers without consent; never buy lists."

The archetype is chosen by keyword scoring over the idea and charter, with an owner override (company setting
`archetype`). The playbook goes into founding prompts and planning cycles.

### 4.4 Critic pass

After the charter stage, a short run (6 steps) with the role "skeptical investor" reads the charter and research
and writes `docs/critique.md`: the three strongest reasons the company fails, and what evidence would answer each.
The plan stage must turn each into a task or record why it accepts the risk. It uses the global setting
`review_model` when set (the strongest model the owner trusts for judgment), otherwise the CEO's model. QA reviews
use `review_model` too.

### 4.5 Planning prompt

The planning prompt starts with the facts, the changes since the last cycle and what waits on the owner, then the
playbook's next experiments, then tasks. New instructions: prefer work that touches real customers (conversations,
signups, replies) over internal documents; every task names the goal it serves and what it should change; never
create a task whose only output is a status file.

## 5. Research quality

### 5.1 Fetched-page cache

`fetch_url` saves the page text it returned to `.cache/pages/<sha256 of URL>.txt` in the company folder and records
it in `fetched(company_id, url, sha, task_id, run_id, fetched_at)`. Pages older than 7 days are pruned.

### 5.2 Verified facts

`record_fact(claim, url, quote)` saves a fact only when the URL was fetched by this company and the quote appears in
that page's cached text (compared case-insensitively, ignoring spacing and punctuation differences). Unverified
facts are refused with the reason ("that quote is not on the page you fetched" or "fetch the page first"). Facts
live in `facts(id, company_id, agent_id, task_id, claim, url, quote, created_at)`, are searchable, and the newest
relevant ones appear in prompts. A fact can be cited in any file as `[fact 12]`.

### 5.3 Source check

`check_sources(path)` lists lines in a document that state a number, price, percentage or named statistic without a
URL that was fetched by this company or a `[fact N]` reference. `finish` runs it for deliverables under `research/`
and for charters: once as "Not finished yet", then the run ends partial with the unsourced lines named. Estimates
labelled "estimate" pass.

## 6. What the owner sees

- **Company Overview:** a "What your agents know" card showing the facts block.
- **Context tab** on each company and **Your context** under Settings: the library, add or paste a file, edit,
  delete, "Always include" with a size meter, and the index status.
- **Search box** on the company page using the same search the agents use.
- **Company Settings:** postal address, archetype override, daily token budget.
- **Global Settings:** review model, embedding model.
- **Doctor:** the index status, companies over their token budget.
- **One-time truth check:** on upgrade, existing company files are scanned with the claims gate, and the owner gets
  one message per company listing files that claim things the records do not support. Files are not changed.

## 7. Data and settings

| Change | Detail |
|---|---|
| `search_index` (FTS5), `index_state`, optional `search_vectors` | section 1.3 |
| `fetched`, `facts` | section 5 |
| `tasks.kind` | section 2.5 |
| `companies.last_cycle_stamp`, `companies.cycle_backoff` | section 3.3 |
| company settings `postal_address`, `archetype`, `daily_token_budget`, `pinned_context`, `founding_stage` | sections 1.2, 2.7, 3.4, 4.1, 4.3 |
| global settings `review_model`, `embedding_model`, `pinned_context` | sections 1.2, 1.3, 4.4 |
| folders `data/context/`, `<company>/context/`, `<company>/records/`, `<company>/.cache/pages/` | sections 1.2, 2.2, 5.1 |

## 8. Modules

| Module | Owns |
|---|---|
| `holdco/context.py` | index, chunking, search, optional embeddings, the context library, prompt sections for pinned context, the context index and relevant memory; tools search, grep, edit_file, read_file changes, list_files summaries |
| `holdco/truth.py` | facts block, records folder, changes since last session, claims gate, action-task rules, write guards, outreach checks, time rule; tools company_status, record_fact, check_sources; fetched-page cache |
| `holdco/planning.py` | question dedupe and cap, structured ask_owner, change stamp and backoff, daily budget, staged founding state machine, charter validation, critic pass, planning prompt sections |
| `holdco/playbooks.py` | archetype playbooks and classification |
| `holdco/checks.py` | placeholder detection, added to audit_page and review_text |
| `holdco/web.py` | relay-proxy block, cached page text hook |

## 9. Tests and gate

Unit tests per module (`tests/test_context.py`, `tests/test_truth.py`, `tests/test_planning.py`) and a replay of
Rooforyou's failure patterns through the real agent loop with a scripted model:

| Scripted behaviour | Expected |
|---|---|
| Writes `docs/deployment_confirmation.md` saying the site is live | refused; no file; the facts say not deployed |
| Writes `docs/email_log.txt` with "Sent email to ..." | refused by the log-name rule and the claims gate |
| Finishes "Send outreach emails" with a file deliverable | not done; done only after a real send, waiting while queued |
| Asks the same deploy question three times in three cycles | one pending question; the others answered "already waiting" |
| Planning cycles with nothing changed and all work waiting | no cycles until something changes; backoff grows |
| Founding with research that never stops | research stage ends at its budget; charter and plan stages still run |
| Charter written with `write_file` | validated and saved to the company record |
| `record_fact` with a quote not on the fetched page | refused |
| `search` for a phrase in an owner context file | found with path and line |
| `edit_file` changes one line of a large page | only that line changes |

Gate: replaying Rooforyou's patterns with the fake model gives no fabricated record accepted, at most one pending
question per topic, and no planning cycles while nothing changes. On the owner's GPU, a new lead-generation company
founds with its charter in the record, tasks created, every price in its research sourced, and a first week that
includes contact with real customers or providers.
