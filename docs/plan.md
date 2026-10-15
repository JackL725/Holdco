# Holdco improvement plan

*October 14, 2026. This is the plan Holdco is being built against; the design documents in this folder
expand each part, and the phase documents under `phases/` record what was built and how to try it.*

## Summary

Holdco should run each company the way a team of 20 copies of one capable operator would: one shared source of
truth, a few measured goals, clear ownership, work checked by tools rather than opinion, and you acting as the
board. Today the agents mostly produce files and report on themselves. The plan below turns that into a closed
loop: real data in, decisions out, results measured.

The five changes that matter most, in order:

1. **A company brain with measured goals.** Every company gets one structured source of truth (goals, metrics,
   customers, decisions, learnings) that every agent reads first and updates through review. Every task links to
   a goal and states the result it expects.
2. **A connector layer built on MCP.** Holdco becomes an MCP client, so any MCP server can plug in. On top of that
   sits a credential vault, a risk tag on every tool (read, write, publish, spend) that drives approvals, and a
   health check for every connector.
3. **Real feedback from the outside world.** Revenue from Stripe, visitors from analytics, replies from email and
   signups from forms flow in as metrics without anyone typing them. Weekly reviews judge work by those numbers,
   not by agents' own summaries.
4. **Verification by tools.** A task counts as done only with evidence: a page rendered in a real browser and
   checked, a test that passed, a link that resolves, an email that was delivered.
5. **A reliability harness for the local model.** A Doctor page with startup preflight checks, a scenario test
   suite that scores any model on your GPU, tighter tool calling, and a watchdog that turns stuck runs into clear
   asks for you instead of silent "partial" results.

You stay the layer on top: a one-page daily brief, approvals from your phone, and a trust ladder that lets
proven, low-risk actions run on their own while spending, legal commitments and anything irreversible always
wait for you.

## How I would run a company with 20 copies of myself

Twenty copies of one mind share a strength and a weakness. They think alike, so they coordinate easily, but they
also make the same mistakes and agree with each other too readily. The operating system has to exploit the first
and guard against the second. These are the rules I would set on day one.

1. **One written source of truth.** No copy remembers what another did. So the company lives in one shared,
   structured record: the goal, the numbers, the customer, the decisions and why. Every copy reads it before
   working and proposes changes to it, rather than keeping private notes that drift.
2. **Few goals, all measurable.** At most three goals at a time, each a number with a date ("10 paying customers
   by Dec 1"). Work that doesn't move a goal doesn't get done. Busy work is the default failure of capable agents
   with no customer in the room.
3. **One owner per outcome, not per activity.** One copy owns "signups" end to end and can pull in others.
   Handoffs lose context, so I would minimise them and make each one a written contract: what is needed, how we
   will know it's right, and by when.
4. **Trust tools, not each other.** A copy reviewing another copy's work shares its blind spots. Checks that
   matter are done by something that isn't a language model: a browser that renders the page, a test that runs,
   an API that confirms the payment link works, a real reply from a real person.
5. **Ship small, measure, decide.** Every bet is written as a hypothesis with a metric and a deadline. Results are
   recorded whether they worked or not, and the next plan starts from those results.
6. **Talk to reality early.** The fastest learning comes from outside: real searches, real competitors' prices,
   real replies to an email. Internal documents are cheap to produce and easy to mistake for progress.
7. **Write down every decision and lesson.** A decision log (what, why, what we expected) and a lessons list stop
   the team relitigating the same choices every planning cycle.
8. **Budgets on everything.** Time, GPU, money, emails per day and posts per week each have a cap. Copies that
   never tire will otherwise spend all of each.
9. **Escalate early, in one place, with a recommendation.** When blocked, a copy asks the owner one clear
   question, with options and its pick. It doesn't wait silently or guess at something irreversible.
10. **The owner is the board, not the bottleneck.** You set direction, approve what is risky or irreversible, and
    read a short brief. Everything else should run without you.

## Where Holdco is today

The core works: agents found a company, plan, hand out tasks, write files and queue outbound actions for
approval. In testing, the automated suite (40 tests) passes on Windows and Linux. Your first real run then showed
where it breaks: the work is real, but the company has no eyes, no scoreboard and no way to tell you something
is wrong.

| Area | Today | Gap |
|---|---|---|
| Knowledge | A charter file, a memory table with keyword search, task results | No single structured record of goals, customers and decisions. Agents rebuild context each run. |
| Goals | KPIs listed in the charter | Not tracked as numbers with dates. Tasks don't link to goals. |
| Metrics | `record_metric`, typed in by agents | Self-reported. Nothing comes from Stripe, analytics or email automatically. |
| Tasks | Priority queue, deliverable and acceptance fields | No dependencies, deadlines or budgets. Parallel agents can overwrite each other's files. |
| Checking work | Static page audit, contrast and text checks, a deliverable check | Nothing renders the site in a browser or tests a signup form or payment link. |
| Connectors | SMTP/IMAP, Netlify, a posting webhook, raw HTTP, DuckDuckGo/SearXNG/Brave search | No MCP, no payments, no analytics and no browser. Keys sit in plain text in the database. |
| Failures | Retries up to 3 times, then marks the task Blocked | Your first run: search was blocked, the CEO gave up with a "partial" note, and nothing told you why. |
| Code execution | Runs Python or shell in the company folder with a timeout | Not sandboxed: it can read anything your user account can. |
| Model | One model for all agents, prompt-based fallback for tool calls | No way to measure how well a given model does Holdco's work before you trust it. |
| You | Dashboard, Inbox, Chat, Thinking tab | Nothing reaches you off the PC. Each approval policy is all or nothing per action type. |

## The operating model to build

The new model is a loop: goals set the work, tools verify it, connectors measure it, and the weekly review turns
results into the next plan. Six pieces make it work. The loop closes through you: the weekly review reaches you
as a board memo, and you set or approve the goals for the next cycle.

### 1. Company brain

The brain is a set of structured records in the database, shown as one page per company, replacing the
free-form charter as the place agents look first.

- **Strategy:** one-liner, customer (ICP), problem, offer, price, positioning, brand voice.
- **Goals:** up to 3 active, each with a metric, a target, a deadline and an owner agent.
- **Metrics:** time series fed by connectors, each with its source ("stripe", "plausible") or marked "manual".
- **Customers and leads:** a small CRM with contacts, companies, stage, last touch, consent and an unsubscribe
  flag.
- **Decisions:** what was decided, why, what result was expected, and a review date.
- **Experiments:** hypothesis, metric, start, end, result, verdict.
- **Lessons:** short rules learned the hard way ("Reddit bans link posts in r/x").

Agents read a compact digest of the brain (under ~1,500 tokens) at the start of every run, and look up the rest
with tools. They change it only through a `propose_brain_change` tool. The CEO reviews proposals during
planning, and a change of price or strategy goes to you.

### 2. Goals, bets and experiments

Planning works top down: goal, then bets that could move it, then tasks. Each bet becomes an experiment with a
stop date. The CEO may not create a task without naming the goal it serves and the expected effect. Tasks that
serve no goal are cancelled during the weekly review.

### 3. Task contracts and a dependency graph

Every task becomes a contract with these fields: goal, owner, inputs, deliverable, acceptance checks (written
as tool checks where possible: "browser_check on /signup returns 200 and the form submits"), depends-on, due
date, and a budget in steps and tokens.

The scheduler runs a task only when its dependencies are done. Agents take a lease on the files they write, so
two agents never edit the same file at once.

### 4. Rhythms

| Cadence | Who | Output |
|---|---|---|
| Every run | Each agent | Reads the brain digest; ends with the structured handoff that exists today |
| Daily, morning | CEO | Plan: top 3 priorities per goal, tasks queued, blockers raised |
| Daily, evening | Analyst | Metrics pulled from connectors, anomalies flagged, one-page brief for you |
| Weekly | CEO + Analyst | Review: goal progress, experiment verdicts, decisions due for review, tasks cancelled; a board memo for you |
| Monthly | CEO | Strategy check against the numbers: keep, change or kill the company, sent to you as a decision |

### 5. Verification

The deliverable check that exists today is extended into a gate that depends on the kind of work:

| Kind of work | Gate |
|---|---|
| Web pages | Rendered in a headless browser, with links, form, console errors, mobile width and a screenshot all checked |
| Code | Its tests run in a sandbox |
| Payment links | Retrieved from the Stripe API |
| Outreach | Checked for compliance (sender identity, opt-out line, daily cap) |
| Research | Every claim cites a fetched URL |

A second agent reviews only what tools can't check, and it gets the tool results, not just the draft.

### 6. Budgets and watchdog

Each company has budgets for GPU hours per day, steps per task, emails per day, posts per week and money. Money
is always $0 unless you approve a purchase. A watchdog spots stuck patterns, such as the same failure 3 times, a
task bouncing between agents, or a connector failing. It stops the loop and sends you one specific question with
a recommended answer.

## Connectors and MCP

Build one connector framework with MCP at its core, then add about a dozen essential connectors. MCP gives Holdco
hundreds of services for the cost of one client, and most of the services a small company needs now publish
official MCP servers.

Agents reach the outside world only through the risk gate. Results come back by polling into the company brain,
which every agent reads at the start of a run.

### The framework

- **MCP client, both protocol generations.** The current spec (2026-07-28) is stateless: no initialize
  handshake, a `server/discover` call, and version and capabilities sent in each request's `_meta`. Many servers
  still speak 2025-11-25, so Holdco probes with `server/discover` and falls back to the old handshake. It
  supports both transports: stdio (Holdco launches `npx` or `docker` servers) and Streamable HTTP for hosted
  servers. JSON-RPC over pipes and HTTP fits Holdco's standard-library-only design.
- **OAuth for hosted servers.** A browser sign-in that returns to 127.0.0.1. Following the spec, tokens are
  stored per issuer and never reused with a different authorization server.
- **A risk tag on every tool.** Tags are read, write (internal), publish, contact (messages a person), spend and
  irreversible. Tags map onto the approval policies that exist today. An MCP tool Holdco hasn't classified
  defaults to "Ask me first". Read tools run freely.
- **Small tool belts.** A local model gets worse as its tool list grows, so MCP tools are never dumped into every
  prompt. Each role gets an allowlist of tools. Everything else is reachable through a `find_tool` search tool
  that loads one tool on demand.
- **A credential vault.** Keys move out of the plain-text settings table: Windows uses DPAPI; Linux uses the
  Secret Service via `secret-tool`; the fallback is a passphrase-protected file. Agents refer to a connector by
  name and never see a key.
- **Health checks.** Every connector has a test that runs on save and hourly. A failing connector shows on the
  Doctor page, alerts you once, and tells agents "Email is down; don't plan work that depends on it". This is the
  fix for what happened in your first run.
- **Inbound data without opening your PC.** Polling comes first: Stripe events, Resend inbound mail, form
  submissions and analytics are all pulled on a schedule, so no port is exposed. Webhooks are optional, through a
  Cloudflare remotely-managed tunnel. Quick Tunnels are for testing only and don't support Server-Sent Events.
- **Metrics pipes.** Each connector can declare metrics it feeds, for example `stripe.mrr`, `plausible.visitors`
  and `resend.replies`. These land in the company brain with their source attached.

### Essential connectors

| Need | Pick | Connect via | Why and guardrails |
|---|---|---|---|
| Payments and revenue | Stripe | Official hosted MCP at mcp.stripe.com, OAuth or an Agent API key | Payment links, products, subscriptions, analytics. Migrate by November 30, 2026: from December the MCP server rejects full-access secret keys and restricted keys without the Agent tag, so create an Agent-tagged key from the start. For digital products, Managed Payments makes Stripe the merchant of record (tax in 80+ countries, fraud, disputes). Refunds and payouts: always ask. |
| Email | Resend + existing SMTP/IMAP | Official resend-mcp (stdio or HTTP) | Sending, inbound mail, broadcasts, contacts, domain DNS setup. Daily caps, opt-out line and sender identity checked before approval. |
| Website | Cloudflare Workers static assets, or keep Netlify | API deploy | Cloudflare's migration guide gives Workers a "distinctly broader set of features" than Pages. Deploys: approval until trusted. |
| Code | GitHub | Official MCP (hosted or Docker) | Repos, issues, PRs. Use `--read-only` for most roles; only Engineering gets write toolsets. |
| Domains | Porkbun | Official MCP (mcp.porkbun.com) or API | Built for agents: scoped keys, dry runs, idempotency, a $100/month default agent spend cap. Purchases: always ask. |
| Analytics | Plausible or self-hosted Umami | Stats API, polled | Visitors, conversions, goals become metrics. Plausible's Stats API is a Business-plan feature on its cloud. |
| Signups and forms | Netlify Forms or a tiny Cloudflare Worker | Polled | Gives the waitlist a real backend, so signups become a metric and CRM leads. |
| Social posting | Buffer | GraphQL API (beta), personal key | One key posts to 11 networks, including X, LinkedIn, Bluesky and Threads. Direct X API is pay-per-use (about $0.015 a post, $0.20 with a link). Keep the existing webhook for Zapier, Make or n8n. |
| Browser | Playwright MCP (Microsoft) | Local stdio | Works from accessibility snapshots, so it needs no vision model. Used for research on JavaScript-heavy sites and for QA of your own pages. Form submits on other sites: always ask. |
| Search | Brave Search API, then SearXNG, then DuckDuckGo (ddgs) | Built in | An automatic fallback chain, so one blocked provider never stalls research again. |
| You, on your phone | Telegram bot (ntfy as a notify-only option) | Built in, long polling | Approve or reject from your phone with buttons. Long polling needs no open port on your PC. |
| Anything else | n8n (self-hosted) | Webhook or its MCP | A catch-all for services without a connector. |

Later, once revenue justifies it: ad platforms (with hard daily caps), accounting (QuickBooks or Xero), CRM sync
(HubSpot or Attio), and local image generation through ComfyUI (scheduled while the language model is idle,
since both share the 5090).

Phase 4 also makes Holdco an MCP server. Claude Desktop, Claude Code or any MCP client can then read your
portfolio, inbox and metrics, or create tasks, without opening the dashboard.

## Making a local model reliable

The biggest risk to Holdco is not missing features but a model that quietly does the wrong thing. On one GPU,
the answer is to measure models on Holdco's own work, narrow what each call has to get right, and recover
automatically when it slips.

1. **Holdco Bench.** A suite of about 30 scripted scenarios run against mock connectors and scored
   automatically. Scenarios include: found a company from an idea; research with a blocked search provider; fix
   a page that fails QA; handle a tool error; don't repeat a failed call; respect an approval; write a compliant
   cold email. A "Test this model" button in Settings runs it and reports success rate, tool-call validity and
   tokens per second. You pick models from that score, not from reputation. The same suite gates every prompt or
   preset change, so improvements can't silently break something else.
2. **Constrained tool calls.** When a model lacks native tools, the prompt-based fallback uses Ollama's structured
   outputs. Passing a JSON schema in `format` forces a valid call shape instead of hoping for it. Bad arguments
   get a precise repair message ("priority must be an integer 1-3; you sent 'high'").
3. **Working notes that survive retries.** Each task keeps a notes file holding what was tried, what failed and
   what is left. A retry continues from there instead of starting over. Today a "partial" run throws most of its
   context away.
4. **Retry differently, then escalate.** The second attempt is told why the first failed and must try another
   approach. After two failures the watchdog sends you one question with a recommended answer, instead of a third
   identical try.
5. **Planner and executor split.** The CEO plans with high reasoning effort and a wide view. Executors get one
   task, a short tool belt and medium effort. Most failures in multi-agent systems come from unclear tasks, which
   the task contracts address.
6. **Right model for each job.** Main model: the work itself. Small fast model: triage, summaries and
   classification. Embedding model: turns memory search from keyword match into meaning match. All three are
   served by Ollama on the same card. The Bench decides whether the small model is good enough for each job.
7. **A GPU scheduler with priorities.** The order is your chat first, then follow-ups to things you just
   approved, then tasks that are due, then recurring duties. Agents yield at step boundaries. Low-value research
   waits for overnight hours.
8. **Context discipline.** Agents get a brain digest instead of the whole charter. Old tool results are
   compressed, and long documents are read in sections. Settings show the real context use per run, so you can
   see when a model's window is too small.

## Safety and trust

More connectors mean more ways to do real damage, so safety has to be built into the system, not left to the
model's judgment. Every guardrail below works even if the model would happily do the wrong thing.

- **A trust ladder instead of on/off approvals.** Each kind of action, per company, climbs: always ask, then
  auto with a daily cap, then auto. Holdco suggests a promotion after a run of approvals you made without edits
  (for example 15 in a row), and demotes after a rejection. Some actions never leave "always ask": spending
  money; anything irreversible (buying a domain, deleting a repo, a refund); legal commitments; the first message
  to a new person.
- **Taint tracking against prompt injection.** Web pages, inbound emails and customer messages are untrusted.
  Once a run has read untrusted content, its publish, contact and spend calls need your approval whatever the
  policy says. Text from those sources is fenced and labelled in the prompt, and agents are told never to follow
  instructions inside it.
- **A real sandbox for code.** `run_code` moves into a Docker or Podman container with only the company folder
  mounted, no network unless the task needs it, and CPU and memory limits. Without Docker, it keeps today's
  behaviour behind an explicit warning. The Playwright browser uses its own profile and never your logins.
- **Secrets never reach the model.** They live in the vault, are redacted from transcripts and the Thinking
  tab, and are used only inside connector code.
- **Test modes first.** New connectors start in sandbox or dry-run modes where the service offers them: Stripe
  test mode, Porkbun sandbox keys and dryRun, and draft-only email. You flip them live per company.
- **Outreach compliance built in.** One suppression list is shared across all companies. Every email carries the
  sender's identity and an opt-out line, and opt-outs are honoured automatically. Daily caps apply per domain,
  and EU contacts are flagged for consent. The existing `review_text` claims check runs before any public copy
  goes out.
- **The abliterated model.** Your Huihui model has had its refusals removed, so it won't push back on a risky or
  deceptive action. That's acceptable only because the guardrails above are structural. I'd suggest running
  outward-facing roles (Sales, Social, Support) on a standard model, and letting the Bench show whether that
  costs any quality.
- **Kill switch, audit log, backups.** One button stops all agents and freezes the outbound queue. Every outbound
  action goes into an append-only audit log, with who proposed it, who approved it and the exact payload sent.
  The data folder gets a nightly backup with 14 days kept.

## Owner experience

You should be able to run a portfolio in about 15 minutes a day, mostly from your phone. Five changes get there.

- **Daily brief.** One page each morning, per company and for the whole portfolio: what moved, with real
  numbers; what shipped; what's stuck; decisions needed, each with a recommendation. It's sent to Telegram and
  pinned on the Portfolio page. A weekly board memo adds goal progress and experiment verdicts.
- **Decisions, separate from approvals.** Approvals are "may I send this?". Decisions are "which of these should
  we do?": options, a recommendation and a deadline after which the agent takes its recommended option, if it's
  reversible. Both can be answered from your phone in one tap.
- **Doctor page.** A green, yellow or red row for each of these, each with a one-click fix or a plain-language
  instruction: model server and model; GPU memory fit; search; each connector; disk space; last backup; last 24
  hours of errors. Founding a company runs the same checks first and refuses to start with a broken dependency,
  telling you why.
- **Portfolio view with capital allocation.** For each company it shows goal progress, revenue, GPU hours used,
  money spent and days since the last real-world result. A monthly portfolio review recommends scaling, holding
  or shutting down each company. GPU time shifts toward the companies with traction.
- **Guided connector setup.** Each connector gets a step-by-step setup card that says where to get the key,
  which permissions to tick (for example a Stripe Agent key, or a Porkbun key without purchase rights), a test
  button, and what agents will be able to do with it.

## Roadmap

Reliability comes before reach. Connecting Stripe to a system that gives up silently would be the wrong order,
so the phases run stabilize, connect, organize, verify, scale. Each phase ends at a gate that must pass on your
PC with your model, not only in tests.

1. **Phase 0: Stabilize.** Doctor page and founding preflight, the search fallback chain, the watchdog with
   escalation, working notes and retry-differently, Telegram notifications and approvals, the kill switch, and
   nightly backups.
   *Gate:* a new company founds with no help from you, and any failure reaches your phone within 5 minutes as
   one clear question.
2. **Phase 0.5: Ground truth and context.** Added after the first real runs: agents see what really happened
   (a facts block and a records folder they cannot edit), claims about deploys, emails and signups are checked
   before they reach you, one open question per topic, no re-planning while nothing changes, staged founding, a
   validated charter, business playbooks, verified research facts, and one search over everything Holdco knows,
   including your own context files. Spec: [phases/phase-0.5-ground-truth-and-context.md](phases/phase-0.5-ground-truth-and-context.md).
   *Gate:* replaying the first run's failures, no fabricated record is accepted, at most one question per topic is
   pending, and no planning runs while nothing changes; on your GPU a new company founds with its charter saved,
   sourced research and a first week that reaches real customers.
3. **Phase 1: Connect.** The MCP client (both protocol generations, stdio and HTTP, OAuth), the vault, risk
   tags, `find_tool`, health checks and metric pipes. First connectors: Stripe, Resend, Playwright MCP, Buffer
   and Brave.
   *Gate:* in Stripe test mode, an agent creates a payment link, a browser check confirms it opens, and a test
   purchase shows up as revenue without anyone typing it.
4. **Phase 2: Organize.** The company brain, goals and experiments, task contracts with dependencies and file
   leases, the daily and weekly rhythms, the daily brief, the board memo and the decisions queue.
   *Gate:* every open task links to a goal, and the weekly review is built from connector data rather than
   agents' summaries.
5. **Phase 3: Verify.** Holdco Bench, the browser QA gate, structured-output tool calls, the code sandbox, taint
   tracking, the trust ladder, and model routing with embeddings.
   *Gate:* the Bench runs in CI against the fake model and on your GPU against real ones, and no site task can be
   marked done without passing the browser check.
6. **Phase 4: Scale.** Holdco as an MCP server, portfolio capital allocation, and the remaining connectors:
   Porkbun, Cloudflare, GitHub, analytics, forms and n8n. Then ComfyUI, ads and accounting when revenue justifies
   them.
   *Gate:* you can run the portfolio for a week from the daily brief and your phone alone.

Each phase ships as its own pull request to the Holdco repo, with tests, so you can try it before the next one
starts.

## Corrections made while building

Checked against the vendor pages on Oct 14, 2026, after the plan was written:

- Stripe's MCP key deadline is **November 30, 2026** (the original plan said Oct 31). From December the server
  rejects full-access secret keys and restricted keys without the Agent tag.
- The MCP 2026-07-28 revision sends the version and capabilities in namespaced `_meta` keys
  (`io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientCapabilities`), requires `Mcp-Method`
  and `Mcp-Name` headers on Streamable HTTP requests, and removes the `Mcp-Session-Id` header; `server/discover`
  is the probe, exactly as the plan says. Details in [connectors.md](connectors.md).

## Sources

All pages were opened on Oct 14, 2026. Prices and terms change, so check them again before you connect a paid
service.

- MCP specification 2026-07-28: changelog
- Stripe: Model Context Protocol (MCP)
- Stripe changelog: Managed Payments, merchant of record
- Resend: official MCP server
- Cloudflare: migrate from Pages to Workers
- Cloudflare: Quick Tunnels (TryCloudflare)
- GitHub MCP server
- Porkbun for AI agents and registering a domain by API
- Plausible Stats API
- Buffer API overview (Zernio, updated May 2026)
- X API pricing (OpenTweet, checked against docs.x.com Oct 10, 2026)
- Playwright MCP overview and repository
- Ollama: structured outputs
- Telegram Bot API
- ntfy: publishing
